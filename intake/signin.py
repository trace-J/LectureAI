"""The panel's sign-in, when it is reached over the web.

The panel listens on this Mac only. Two roads lead to it from elsewhere:
the account service's relay (relay.py), where the service itself is the
sign-in and names the viewer in every request, and a Cloudflare Tunnel,
where anyone who arrives has to sign in here first. This module is the
gate for both and the sign-in for the second. Requests from this Mac carry no Cloudflare headers and are
never gated, so http://127.0.0.1:5173 keeps working whatever the tunnel is
doing.

Who may enter is decided by the Syllabus account this Mac is signed in to
(account.py): its owner, and nobody else. The sign-in itself happens on the
account service, which holds the Google login; the panel never talks to
Google here.

    /login             remembers where you were going, sends you to the
                       account service's /panel/authorize
    /account/callback  trades the one-time code the service sent back for
                       the account, using this panel's device token, and
                       sets the session cookie
    /logout            clears the cookie

A Mac that is not signed in to an account refuses every request that came
through the tunnel with a 503 saying so, so a tunnel that is up before the
Mac is signed in exposes nothing. (Until 2026-09-14 the panel could also run
Google's sign-in itself, against an allowlist of addresses in .env; that path
and its PANEL_GOOGLE_* and PANEL_ALLOWED_EMAILS settings are gone.)

The session is a signed cookie naming the account and the email, good for
30 days. It is signed with a key the panel generates once and keeps in the
profile's .work folder, so a restart does not sign everyone out;
PANEL_SECRET_KEY in .env overrides that, for anyone who wants to manage the
key themselves. A session stops counting the moment this Mac is signed out
of the account, or signed in to a different one.
"""

from __future__ import annotations

import secrets
import sys
from urllib.parse import urlencode

from flask import Blueprint, Flask, g, jsonify, make_response, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from intake import account, config, relay

bp = Blueprint("signin", __name__)

ACCOUNT_CALLBACK_PATH = "/account/callback"

SESSION_COOKIE = "syllabus_session"
SESSION_DAYS = 30
# The state of each attempt lives in a short-lived cookie between /login and
# the callback. Ten minutes is plenty to pick an account.
FLOW_COOKIE = "syllabus_signin"
FLOW_SECONDS = 600

# Routes that have to be reachable before there is a session.
OPEN_PATHS = {"/login", ACCOUNT_CALLBACK_PATH, "/logout"}

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
    config.write_private(path, key + "\n")
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

def configured() -> bool:
    """Whether the web sign-in can work: this Mac is signed in to an account."""
    return account.enabled() and account.load() is not None


def mode() -> str:
    """How the web sign-in works right now: "account", or "" for not at all.

    Kept as a function with a name, because the doctor and the tests read
    it, and because the answer used to have a second value.
    """
    return "account" if configured() else ""


# --- The request at hand -----------------------------------------------------

def via_relay() -> bool:
    """Whether this request arrived over the account service's relay.

    relay.py runs a relayed request against the app in process and marks
    the WSGI environ. A request from the network can set HTTP_* keys and
    nothing else, so the mark cannot be forged from outside.
    """
    return bool(request.environ.get(relay.RELAY_KEY))


def via_cloudflare() -> bool:
    """Whether this request came in through Cloudflare's edge.

    Cloudflare stamps every request it forwards with Cf-Ray, and a client
    cannot remove it, so its absence means the request never left this Mac.
    """
    return "Cf-Ray" in request.headers


def _external() -> bool:
    """Whether the browser is talking to us over https (through the tunnel)."""
    return via_cloudflare() or via_relay() \
        or request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure


def _public_base() -> str:
    """The address this panel is published at, as the account service should
    see it: PANEL_PUBLIC_URL through the tunnel, else the request's own
    origin.

    PANEL_PUBLIC_URL is needed because this tunnel's ingress rewrites the
    Host header to 127.0.0.1:5173 on the way in, so the panel cannot learn
    its public hostname from the request. Left unset, or in the dev
    preview, the request's own host is used.
    """
    if via_cloudflare() and config.PANEL_PUBLIC_URL:
        return config.PANEL_PUBLIC_URL.strip().rstrip("/")
    scheme = "https" if _external() else "http"
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{scheme}://{host}"


def _safe_next(value: str | None) -> str:
    """A path on this panel to return to after signing in, never elsewhere."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def viewer() -> str:
    """Who the session cookie says is signed in, or '' when nobody is.

    The session has to name the account this Mac is signed in to: a cookie
    issued for a previous account, or before this Mac had one, is nobody,
    and so is a forged or expired one.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return ""
    try:
        data = _signer().loads(raw, max_age=SESSION_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return ""
    email = str(data.get("email", "")).lower()
    acct = account.load() if account.enabled() else None
    if not email or acct is None:
        return ""
    return email if data.get("account_id") == acct.account_id else ""


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


NOT_SET_UP = ("This panel is reachable through Cloudflare, but the Mac that runs it "
              "is not signed in to a Syllabus account, so nobody can sign in here. "
              "On that Mac, open the Setup page and choose Sign in to a Syllabus account.")


# --- Requests from somewhere else on the web ---------------------------------

# Methods that can change something. GET and HEAD are deliberately not here:
# a page elsewhere can make a browser issue those whatever we do, and what
# keeps it from reading the answer is the same-origin policy, not us.
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# What the panel's own pages send on every call. This is the load-bearing
# check: a browser will only send form-urlencoded, multipart or text/plain to
# another origin without asking permission first, and the panel answers no
# such question, so requiring JSON is on its own enough to stop a page on
# another site from acting here.
JSON_TYPE = "application/json"

# Names this Mac answers to. Anything else means a browser resolved somebody
# else's hostname to 127.0.0.1 and now believes their page and this panel are
# the same origin, which is DNS rebinding.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# Set by `intake panel --expose`, which is an explicit decision to serve
# something other than this Mac. The name the panel is then reached by is
# whatever the operator put in front of it, so there is nothing here to
# compare a Host against. The checks that do not depend on Host still run.
EXPOSED = False


def _host_name() -> str:
    """The host the request was addressed to, without its port."""
    host = request.headers.get("Host", "")
    if host.startswith("[") and "]" in host:      # [::1]:5173
        return host[:host.index("]") + 1]
    return host.rsplit(":", 1)[0] if ":" in host else host


def _our_origins() -> set[str]:
    """The origins the panel's own pages are served from."""
    host = request.headers.get("Host", "")
    if not host:
        return set()
    return {f"http://{host}".lower(), f"https://{host}".lower()}


def cross_site() -> str:
    """Why this request looks like it came from another site, or "".

    The panel has no authentication for local requests, by design: anything
    running as this user could do all of it directly anyway. A web page in a
    browser is not that. It cannot read the panel's answers, but until now it
    could still make the panel act — start the microphone, spawn a watcher,
    SIGTERM one, throw away the Drive token, sign the Mac out — by posting a
    plain form at it, which needs nobody's permission.

    A client that is not a browser sends no Origin and no Sec-Fetch-Site.
    That is allowed on purpose: curl and the CLI are not what is being
    defended against, and they cannot be tricked into acting on behalf of a
    page somebody else wrote.
    """
    if via_relay():
        # The account service built this request in process. It carries no
        # browser headers at all, and the service's own gate already decided
        # who is allowed to reach this panel.
        return ""

    host = _host_name().lower()
    if host and not EXPOSED and host not in LOCAL_HOSTS and not via_cloudflare():
        return (f"this panel answers to localhost on this Mac, and {host} is "
                f"somebody else's name for it")

    if request.method not in UNSAFE_METHODS:
        return ""
    if request.headers.get("Sec-Fetch-Site", "").lower() in ("cross-site", "same-site"):
        return "that came from another site"
    origin = request.headers.get("Origin", "")
    if origin and origin.lower() not in _our_origins():
        return f"that came from {origin}"
    kind = (request.content_type or "").split(";")[0].strip().lower()
    if kind != JSON_TYPE:
        return (f"this panel only takes {JSON_TYPE}, which a page on another "
                f"site cannot send here without asking permission first")
    return ""


# --- The gate ----------------------------------------------------------------

def relay_viewer() -> str:
    """Who the relay says is looking, if that is this Mac's account's owner.

    The service only relays the owner, so a mismatch means account.json
    changed under a socket that belonged to the previous account. Nobody
    then.
    """
    acct = account.load() if account.enabled() else None
    email = str(request.environ.get(relay.VIEWER_KEY, "")).lower()
    if acct is None or not email:
        return ""
    return email if request.environ.get(relay.VIEWER_ACCOUNT_KEY) == acct.account_id else ""


def gate():
    """Run before every request. None lets it through."""
    g.viewer = ""
    g.relayed = False
    # Before anything about who is signed in: a request that came from
    # another site is refused whether it is local, tunnelled or relayed, and
    # whether or not anybody is signed in here.
    elsewhere = cross_site()
    if elsewhere:
        return refuse(403, f"Refused: {elsewhere}.")
    if via_relay():
        # The account service already decided who may reach this panel's
        # address and named them in the frame; that is the sign-in.
        who = relay_viewer()
        if not who:
            return refuse(403, "That account does not own this Syllabus.")
        g.viewer = who
        g.relayed = True
        return None
    if not via_cloudflare():
        return None
    if not configured():
        return refuse(503, NOT_SET_UP)
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
    """Send the browser to the account service, which knows who owns this Mac."""
    acct = account.load() if account.enabled() else None
    if acct is None:
        return _page(NOT_SET_UP, 503)
    state = secrets.token_urlsafe(24)
    base = _public_base()
    # Registered on every sign-in rather than once, so a panel whose address
    # changed (a new hostname, a new PANEL_PUBLIC_URL) heals itself. A
    # failure is logged and the sign-in goes ahead: the service will refuse
    # the redirect and say why, which is a better message than none.
    account.register_public_url(base)
    _say(f"sign-in started through the account service; it will send the "
         f"browser back to {base}{ACCOUNT_CALLBACK_PATH}")
    flow = _signer().dumps({"state": state, "next": _safe_next(request.args.get("next"))})
    params = {"device": acct.device_id, "redirect_uri": base + ACCOUNT_CALLBACK_PATH,
              "state": state}
    resp = make_response(redirect(account.url() + "/panel/authorize?" + urlencode(params)))
    return _set_cookie(resp, FLOW_COOKIE, flow, FLOW_SECONDS)


@bp.get(ACCOUNT_CALLBACK_PATH)
def account_callback():
    acct = account.load() if account.enabled() else None
    if acct is None:
        return _page(NOT_SET_UP, 503)
    raw = request.cookies.get(FLOW_COOKIE, "")
    try:
        flow = _signer().loads(raw, max_age=FLOW_SECONDS) if raw else None
    except (BadSignature, SignatureExpired):
        flow = None
    if not flow or request.args.get("state") != flow.get("state"):
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


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    resp = make_response(_page("You are signed out.", 200, ("/login", "Sign in")))
    _clear_cookie(resp, SESSION_COOKIE)
    return _clear_cookie(resp, FLOW_COOKIE)


#: Paths whose answers name the account, describe the settings, or carry a
#: credential. Nothing here should sit in a cache: not the browser's, and not
#: whatever is between a phone and this Mac when the panel is reached through
#: the relay.
_NO_STORE_PREFIXES = ("/api/setup", "/api/account", "/api/drive", "/api/doctor",
                      "/setup", "/signin", "/account")


def no_store(response):
    """Mark answers that are about this install rather than about the app."""
    path = request.path
    if any(path == p or path.startswith(p + "/") for p in _NO_STORE_PREFIXES):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Pragma", "no-cache")
    return response


def install(app: Flask) -> None:
    app.register_blueprint(bp)
    app.before_request(gate)
    app.after_request(no_store)
