"""The panel's sign-in, when it is reached over the web.

The panel listens on this Mac only, and a Cloudflare Tunnel carries
`syllabus.maincoursemedia.com` to it. Anyone who arrives that way has to
sign in first. Requests from this Mac carry no Cloudflare headers and are
never gated, so http://127.0.0.1:5173 keeps working whatever the tunnel is
doing.

Who may enter depends on whether this Mac has been signed in to a Syllabus
account (account.py). When it has, the account's owner is the one person
allowed, and the sign-in goes through the account service:

    /login             remembers where you were going, sends you to the
                       account service's /panel/authorize
    /account/callback  trades the one-time code the service sent back for
                       the account, using this panel's device token, and
                       sets the session cookie

When it has not, the older path still works: Google's sign-in directly,
with the Web client in .env, and only the addresses in PANEL_ALLOWED_EMAILS
may enter:

    /login            sends you to Google
    /oauth2/callback  trades Google's code for an ID token, checks it, and
                      sets the session cookie

    /logout           clears the cookie, either way

The session is a signed cookie holding the email and when it was issued. It
is signed with a key the panel generates once and keeps in the profile's
.work folder, so a restart does not sign everyone out. PANEL_SECRET_KEY in
.env overrides that, for anyone who wants to manage the key themselves.

The Web OAuth client this uses is a different one from the Desktop client
`intake login` authorizes Drive with: Google only lets a Web client redirect
to a hostname, and only a Desktop client's secret is safe to ship inside the
package. They live in different Cloud projects: the Web client in the one
named LectureAI, the Desktop client in friendly-bazaar-507320-b7.
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

from intake import account, config

bp = Blueprint("signin", __name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/oauth2/callback"
ACCOUNT_CALLBACK_PATH = "/account/callback"

SESSION_COOKIE = "syllabus_session"
SESSION_DAYS = 30
# The state and nonce live in a short-lived cookie between /login and the
# callback. Ten minutes is plenty to pick an account.
FLOW_COOKIE = "syllabus_signin"
FLOW_SECONDS = 600

# Routes that have to be reachable before there is a session.
OPEN_PATHS = {"/login", CALLBACK_PATH, ACCOUNT_CALLBACK_PATH, "/logout"}

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


def google_configured() -> bool:
    """Whether the direct Google sign-in has its three settings."""
    return bool(config.PANEL_GOOGLE_CLIENT_ID and config.PANEL_GOOGLE_CLIENT_SECRET
                and allowed_emails())


def mode() -> str:
    """How the web sign-in works right now: "account", "google", or ""."""
    if account.enabled() and account.load() is not None:
        return "account"
    return "google" if google_configured() else ""


def configured() -> bool:
    """Whether the web sign-in has everything it needs, one way or the other."""
    return bool(mode())


def missing() -> list[str]:
    """The .env names still empty, for the 503 page and `intake doctor`.

    Only meaningful when mode() is "": with an account, none of these matter.
    """
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
    """Where Google sends the browser back.

    Through the tunnel that is PANEL_PUBLIC_URL, the address the panel is
    published at, because the tunnel rewrites the Host header to the origin
    (127.0.0.1:5173) on the way in and Google has never heard of that. Left
    unset, or in the dev preview, it is the request's own host, https if it
    came that way. Whatever it comes out as has to be registered, exactly,
    on the Web client.
    """
    return _public_base() + CALLBACK_PATH


def _safe_next(value: str | None) -> str:
    """A path on this panel to return to after signing in, never elsewhere."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _public_base() -> str:
    """The address this panel is published at, as Google or the account
    service should see it: PANEL_PUBLIC_URL through the tunnel, else the
    request's own origin."""
    if via_cloudflare() and config.PANEL_PUBLIC_URL:
        return config.PANEL_PUBLIC_URL.strip().rstrip("/")
    scheme = "https" if _external() else "http"
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{scheme}://{host}"


def viewer() -> str:
    """Who the session cookie says is signed in, or '' when nobody is.

    With an account, the session has to name that account: a cookie issued
    to an address on the old allowlist stops counting the moment this Mac is
    signed in, and one issued for a previous account does too. Without an
    account, the email has to be on PANEL_ALLOWED_EMAILS. Either way a
    forged or expired cookie is nobody.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return ""
    try:
        data = _signer().loads(raw, max_age=SESSION_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return ""
    email = str(data.get("email", "")).lower()
    if not email:
        return ""
    acct = account.load() if account.enabled() else None
    if acct is not None:
        return email if data.get("account_id") == acct.account_id else ""
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
        return refuse(503, _not_set_up())
    if request.path in OPEN_PATHS:
        return None
    who = viewer()
    if not who:
        return refuse(401, "Sign in first.", request.full_path.rstrip("?"))
    g.viewer = who
    return None


def _not_set_up() -> str:
    names = ", ".join(missing())
    return ("This panel is reachable through Cloudflare, but its sign-in is not "
            "set up. On the Mac that runs it, open the Setup page and sign in to "
            "a Syllabus account, or set " + names + " in its .env and restart it.")


# --- The routes --------------------------------------------------------------

@bp.get("/login")
def login():
    how = mode()
    if how == "account":
        return _login_via_account()
    if how != "google":
        return _page(_not_set_up(), 503)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    # Logged on purpose: when Google says the request is invalid, this is
    # the value that has to appear, exactly, in the Web client's redirect URIs.
    _say(f"sign-in started; Google will be sent back to {_redirect_uri()}")
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


def _login_via_account():
    """Send the browser to the account service, which knows who owns this Mac."""
    acct = account.load()
    state = secrets.token_urlsafe(24)
    base = _public_base()
    # Registered on every sign-in rather than once, so a panel whose address
    # changed (a new hostname, a new PANEL_PUBLIC_URL) heals itself. A
    # failure is logged and the sign-in goes ahead: the service will refuse
    # the redirect and say why, which is a better message than none.
    account.register_public_url(base)
    _say(f"sign-in started through the account service; it will send the "
         f"browser back to {base}{ACCOUNT_CALLBACK_PATH}")
    flow = _signer().dumps({"state": state, "via": "account",
                            "next": _safe_next(request.args.get("next"))})
    params = {"device": acct.device_id, "redirect_uri": base + ACCOUNT_CALLBACK_PATH,
              "state": state}
    resp = make_response(redirect(account.url() + "/panel/authorize?" + urlencode(params)))
    return _set_cookie(resp, FLOW_COOKIE, flow, FLOW_SECONDS)


@bp.get(ACCOUNT_CALLBACK_PATH)
def account_callback():
    acct = account.load() if account.enabled() else None
    if acct is None:
        return _page("This panel is not signed in to a Syllabus account.", 503)
    raw = request.cookies.get(FLOW_COOKIE, "")
    try:
        flow = _signer().loads(raw, max_age=FLOW_SECONDS) if raw else None
    except (BadSignature, SignatureExpired):
        flow = None
    if not flow or flow.get("via") != "account" \
            or request.args.get("state") != flow.get("state"):
        return _page("That sign-in took too long or did not start here. "
                     "Try again.", 400, ("/login", "Sign in"))
    code = request.args.get("code", "")
    if not code:
        return _page("The account service sent no code back.", 400, ("/login", "Try again"))
    try:
        who = account.exchange_code(code)
    except Exception as exc:
        _say(f"sign-in failed: could not reach the account service: {exc}")
        return _page("The account service could not be reached to finish the "
                     "sign-in.", 502, ("/login", "Try again"))
    if who is None:
        _say("sign-in failed: the account service refused the code")
        return _page("That sign-in could not be confirmed. Try again.", 400,
                     ("/login", "Sign in"))
    email = str(who.get("email", "")).lower()
    if str(who.get("id", "")) != acct.account_id or not email:
        _say(f"refused a sign-in for {email or 'an unknown account'}: not this "
             f"Mac's account")
        return _page("That account does not own this Syllabus.", 403,
                     ("/login", "Try again"))
    _say(f"signed in through the account: {email}")
    session = _signer().dumps({"email": email, "account_id": acct.account_id})
    resp = make_response(redirect(_safe_next(flow.get("next"))))
    _clear_cookie(resp, FLOW_COOKIE)
    return _set_cookie(resp, SESSION_COOKIE, session, SESSION_DAYS * 86400)


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
    if not google_configured():
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
