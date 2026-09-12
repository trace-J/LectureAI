"""The panel's sign-in, when it is reached over the web.

The panel has no accounts of its own. It listens on this Mac only, and a
Cloudflare Tunnel carries `syllabus.maincoursemedia.com` to it. Anyone who
arrives that way has to sign in with Google first, and their Google account
has to be one of the addresses named in PANEL_ALLOWED_EMAILS. Requests from
this Mac carry no Cloudflare headers and are never gated, so
http://127.0.0.1:5173 keeps working whatever the tunnel is doing.

The flow is plain OpenID Connect against Google, with nothing but `requests`:

    /login            remembers where you were going, sends you to Google
    /oauth2/callback  trades Google's code for an ID token, checks it, and
                      sets the session cookie
    /logout           clears the cookie

The session is a signed cookie holding the email and when it was issued. It
is signed with a key the panel generates once and keeps in the profile's
.work folder, so a restart does not sign everyone out. PANEL_SECRET_KEY in
.env overrides that, for anyone who wants to manage the key themselves.

The Web OAuth client this uses is a different one from the Desktop client
`intake login` authorizes Drive with: Google only lets a Web client redirect
to a hostname, and only a Desktop client's secret is safe to ship inside the
package. Both belong to the same Cloud project.
"""

from __future__ import annotations

import secrets
import sys
from urllib.parse import urlencode

import requests
from flask import Blueprint, Flask, g, jsonify, make_response, redirect, request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from intake import config

bp = Blueprint("signin", __name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/oauth2/callback"

SESSION_COOKIE = "syllabus_session"
SESSION_DAYS = 30
# The state and nonce live in a short-lived cookie between /login and the
# callback. Ten minutes is plenty to pick an account.
FLOW_COOKIE = "syllabus_signin"
FLOW_SECONDS = 600

# Routes that have to be reachable before there is a session.
OPEN_PATHS = {"/login", CALLBACK_PATH, "/logout"}

_serializer: URLSafeTimedSerializer | None = None


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --- The signing key ---------------------------------------------------------

def secret_key() -> str:
    """PANEL_SECRET_KEY from .env, else a key generated once into .work."""
    if config.PANEL_SECRET_KEY:
        return config.PANEL_SECRET_KEY
    path = config.WORK_DIR / "panel-secret"
    if path.exists():
        key = path.read_text().strip()
        if key:
            return key
    key = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n")
    path.chmod(0o600)
    return key


def _signer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(secret_key(), salt="panel-signin")
    return _serializer


def reset() -> None:
    """Forget the cached signer, so the next request re-reads the key."""
    global _serializer
    _serializer = None


# --- What is configured ------------------------------------------------------

def allowed_emails() -> set[str]:
    return {e.strip().lower() for e in config.PANEL_ALLOWED_EMAILS.split(",")
            if e.strip()}


def configured() -> bool:
    """Whether the web sign-in has everything it needs."""
    return bool(config.PANEL_GOOGLE_CLIENT_ID and config.PANEL_GOOGLE_CLIENT_SECRET
                and allowed_emails())


def missing() -> list[str]:
    """The .env names still empty, for the 503 page and `intake doctor`."""
    out = []
    if not config.PANEL_GOOGLE_CLIENT_ID:
        out.append("PANEL_GOOGLE_CLIENT_ID")
    if not config.PANEL_GOOGLE_CLIENT_SECRET:
        out.append("PANEL_GOOGLE_CLIENT_SECRET")
    if not allowed_emails():
        out.append("PANEL_ALLOWED_EMAILS")
    return out


# --- The request at hand -----------------------------------------------------

def via_cloudflare() -> bool:
    """Whether this request came in through Cloudflare's edge.

    Cloudflare stamps every request it forwards with Cf-Ray, and a client
    cannot remove it, so its absence means the request never left this Mac.
    """
    return "Cf-Ray" in request.headers


def _external() -> bool:
    """Whether the browser is talking to us over https (through the tunnel)."""
    return via_cloudflare() or request.headers.get("X-Forwarded-Proto") == "https" \
        or request.is_secure


def _redirect_uri() -> str:
    """Where Google sends the browser back: this same host, https if it came
    that way. Through the tunnel that is the published hostname; in the
    dev preview it is 127.0.0.1 and its port. Both are registered on the
    Web client."""
    scheme = "https" if _external() else "http"
    return f"{scheme}://{request.host}{CALLBACK_PATH}"


def _safe_next(value: str | None) -> str:
    """A path on this panel to return to after signing in, never elsewhere."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def viewer() -> str:
    """Who the session cookie says is signed in, or '' when nobody is.

    A cookie that is forged, expired, or names someone since removed from
    PANEL_ALLOWED_EMAILS counts as nobody.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return ""
    try:
        data = _signer().loads(raw, max_age=SESSION_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return ""
    email = str(data.get("email", "")).lower()
    return email if email in allowed_emails() else ""


def _set_cookie(resp, name: str, value: str, max_age: int):
    resp.set_cookie(name, value, max_age=max_age, httponly=True,
                    secure=_external(), samesite="Lax", path="/")
    return resp


def _clear_cookie(resp, name: str):
    resp.delete_cookie(name, path="/")
    return resp


def _page(message: str, code: int = 200, link: tuple[str, str] | None = None):
    extra = f"<p><a href='{link[0]}'>{link[1]}</a></p>" if link else ""
    return (f"<!doctype html><title>{config.PROFILE.title}</title>"
            f"<div style='font: 15px/1.5 system-ui; max-width: 40em; margin: 4em auto'>"
            f"<p>{message}</p>{extra}</div>"), code


def refuse(code: int, message: str, next_path: str | None = None):
    """A refusal shaped for the caller: JSON for the API, a page otherwise.

    A 401 on a page is a redirect to /login instead, since that is what a
    person in a browser needs; the API gets the status so the page's polling
    can notice the session ended.
    """
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), code
    if code == 401:
        return redirect("/login?" + urlencode({"next": _safe_next(next_path)}))
    return _page(message, code)


# --- The gate ----------------------------------------------------------------

def gate():
    """Run before every request. None lets it through."""
    g.viewer = ""
    if not via_cloudflare():
        return None
    if not configured():
        names = ", ".join(missing())
        return refuse(503, "This panel is reachable through Cloudflare, but its "
                           "sign-in is not set up. Set " + names + " in its .env "
                           "and restart it.")
    if request.path in OPEN_PATHS:
        return None
    who = viewer()
    if not who:
        return refuse(401, "Sign in first.", request.full_path.rstrip("?"))
    g.viewer = who
    return None


# --- The routes --------------------------------------------------------------

@bp.get("/login")
def login():
    if not configured():
        return _page("This panel's sign-in is not set up: " + ", ".join(missing())
                     + " are empty in its .env.", 503)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    flow = _signer().dumps({"state": state, "nonce": nonce,
                            "next": _safe_next(request.args.get("next"))})
    params = {
        "client_id": config.PANEL_GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        # Always show the account picker: the person may have several Google
        # accounts and only one of them is on the list.
        "prompt": "select_account",
    }
    resp = make_response(redirect(AUTH_URL + "?" + urlencode(params)))
    return _set_cookie(resp, FLOW_COOKIE, flow, FLOW_SECONDS)


def exchange_code(code: str, redirect_uri: str) -> str:
    """Trade the authorization code for Google's ID token (a JWT string)."""
    res = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": config.PANEL_GOOGLE_CLIENT_ID,
        "client_secret": config.PANEL_GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=15)
    res.raise_for_status()
    return res.json()["id_token"]


def verify_id_token(token: str) -> dict:
    """The claims of an ID token Google issued to our client, or an error.

    google-auth fetches Google's published keys (and caches them), checks
    the signature, the issuer, the audience, and the expiry.
    """
    return google_id_token.verify_oauth2_token(
        token, google_requests.Request(), config.PANEL_GOOGLE_CLIENT_ID)


@bp.get(CALLBACK_PATH)
def callback():
    if not configured():
        return _page("This panel's sign-in is not set up.", 503)
    raw = request.cookies.get(FLOW_COOKIE, "")
    try:
        flow = _signer().loads(raw, max_age=FLOW_SECONDS) if raw else None
    except (BadSignature, SignatureExpired):
        flow = None
    if not flow or request.args.get("state") != flow.get("state"):
        return _page("That sign-in took too long or did not start here. "
                     "Try again.", 400, ("/login", "Sign in"))
    if request.args.get("error"):
        _say(f"sign-in refused by Google: {request.args['error']}")
        return _page("Google did not complete the sign-in.", 400,
                     ("/login", "Try again"))
    code = request.args.get("code", "")
    if not code:
        return _page("Google sent no code back.", 400, ("/login", "Try again"))
    try:
        claims = verify_id_token(exchange_code(code, _redirect_uri()))
    except Exception as exc:  # network, a bad code, a token for another client
        _say(f"sign-in failed: {type(exc).__name__}: {exc}")
        return _page("The sign-in could not be checked with Google.", 502,
                     ("/login", "Try again"))
    if claims.get("nonce") != flow.get("nonce"):
        return _page("That sign-in did not match the one that was started.", 400,
                     ("/login", "Try again"))
    email = str(claims.get("email", "")).lower()
    if not claims.get("email_verified") or email not in allowed_emails():
        _say(f"refused a sign-in from {email or 'an account with no email'}")
        return _page(f"{email or 'That account'} is not on the list for this "
                     f"panel.", 403, ("/login", "Use a different account"))
    _say(f"signed in: {email}")
    session = _signer().dumps({"email": email})
    resp = make_response(redirect(_safe_next(flow.get("next"))))
    _clear_cookie(resp, FLOW_COOKIE)
    return _set_cookie(resp, SESSION_COOKIE, session, SESSION_DAYS * 86400)


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    resp = make_response(_page("You are signed out.", 200, ("/login", "Sign in")))
    _clear_cookie(resp, SESSION_COOKIE)
    return _clear_cookie(resp, FLOW_COOKIE)


def install(app: Flask) -> None:
    app.register_blueprint(bp)
    app.before_request(gate)
