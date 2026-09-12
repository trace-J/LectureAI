"""A Syllabus account, and this panel's place in it.

Accounts live in a small web service (syllabusaccounts.maincoursemedia.com,
the syllabus-accounts repo). This module is the panel's side of it: claiming
an identity for the Mac the panel runs on, remembering it, and asking the
service who this panel is.

Claiming works like signing a TV in to a streaming service. The panel asks
the service for a code, shows it on the Setup page, and the person types it
into the service's website while signed in there with Google. The panel
polls until the service says the code was approved, and receives a token
that identifies this Mac from then on. That token, and who it belongs to,
is kept in account.json in the profile's home. The browser never sees the
token and the panel never sees the person's Google session.

None of this is required. With no account.json the panel is exactly what it
was: a single-user local tool. ACCOUNTS_URL=off in .env turns the feature
off altogether, and the Setup page then does not mention it.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass

import requests

from intake import config

OFF_WORDS = {"off", "none", "no", "false", "0"}
TIMEOUT = 15
# If the service says nothing about how often to poll, this is how often.
DEFAULT_INTERVAL = 5
DEFAULT_CODE_SECONDS = 900


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Account:
    """What account.json holds: the token and who it says this Mac is."""
    token: str
    account_id: str
    email: str
    name: str
    device_id: str
    device_name: str
    profile: str
    url: str
    claimed_at: str

    def public(self) -> dict:
        """The parts a page may see. Never the token."""
        return {"email": self.email, "name": self.name, "device_name": self.device_name,
                "device_id": self.device_id, "claimed_at": self.claimed_at}


# --- Settings and the file --------------------------------------------------

def url() -> str:
    return (config.ACCOUNTS_URL or "").strip().rstrip("/")


def enabled() -> bool:
    """Whether accounts are a thing for this install at all."""
    value = url()
    return bool(value) and value.lower() not in OFF_WORDS


def load() -> Account | None:
    """The account this panel was claimed into, or None. A broken file is None too."""
    path = config.ACCOUNT_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Account(**{k: str(data.get(k, "")) for k in Account.__dataclass_fields__})
    except (OSError, ValueError, TypeError):
        return None


def save(account: Account) -> None:
    path = config.ACCOUNT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(account), indent=2) + "\n")
    path.chmod(0o600)


def forget() -> None:
    try:
        config.ACCOUNT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def device_name() -> str:
    """What this Mac calls itself, for the account page: 'Trace's MacBook Pro'."""
    try:
        out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                             text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname().split(".")[0] or "A Mac"


# --- Talking to the service -------------------------------------------------

def _http(method: str, full_url: str, headers: dict, body: dict | None,
          timeout: float) -> tuple[int, dict]:
    """One request; status and the JSON body (or {} when there is none).

    Replaced wholesale by the tests, which is why the whole exchange goes
    through this one function.
    """
    res = requests.request(method, full_url, headers=headers, json=body, timeout=timeout)
    try:
        data = res.json()
    except ValueError:
        data = {}
    return res.status_code, data if isinstance(data, dict) else {}


transport = _http


def call(method: str, path: str, body: dict | None = None,
         token: str | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return transport(method, url() + path, headers, body, TIMEOUT)


def summary() -> dict:
    """For the dashboard's status poll: no network, just the file."""
    if not enabled():
        return {"enabled": False, "signed_in": False}
    acct = load()
    out = {"enabled": True, "url": url(), "signed_in": acct is not None}
    if acct:
        out.update(acct.public())
    return out


def whoami() -> tuple[str, str]:
    """Ask the service about this panel's token.

    Returns ("ok", email), ("revoked", why), or ("unreachable", why). A
    revoked token is forgotten here, so the Setup page can offer a fresh
    sign-in rather than a token the service will never accept again.
    """
    acct = load()
    if acct is None:
        return "none", "not signed in"
    try:
        status, data = call("GET", "/me", token=acct.token)
    except Exception as exc:  # network down, DNS, a 5xx that was not JSON
        return "unreachable", f"{type(exc).__name__}: {exc}"
    if status == 401:
        _say(f"the account service no longer accepts this Mac's token; forgetting it")
        forget()
        return "revoked", "this Mac was removed from the account"
    if status != 200:
        return "unreachable", f"the account service answered {status}"
    account = data.get("account") or {}
    return "ok", str(account.get("email") or acct.email)


def sign_out() -> None:
    """Tell the service to drop this Mac's token, then forget it here.

    Best effort on the network: a service that cannot be reached still
    leaves this Mac signed out, and the token can be removed from the
    account page later.
    """
    acct = load()
    if acct is not None:
        try:
            call("POST", "/device/revoke", {}, token=acct.token)
        except Exception as exc:
            _say(f"could not tell the account service to drop the token: {exc}")
    forget()


# --- Claiming this Mac ------------------------------------------------------

_claim: dict = {"running": False, "user_code": "", "verification_uri": "",
                "verification_uri_complete": "", "expires_at": 0.0, "error": ""}
_claim_lock = threading.Lock()


def claim_status() -> dict:
    with _claim_lock:
        out = dict(_claim)
    out["expires_in"] = max(0, round(out["expires_at"] - time.time())) if out["running"] else 0
    return out


def _set_claim(**fields) -> None:
    with _claim_lock:
        _claim.update(fields)


def start_claim(name: str | None = None, background: bool = True) -> dict:
    """Ask the service for a code and start polling for its approval.

    Returns the code and where to enter it. With background=False the poll
    loop is not started, so a caller (a test) can drive it with run_claim.
    """
    if not enabled():
        raise RuntimeError("accounts are turned off (ACCOUNTS_URL)")
    if load() is not None:
        raise RuntimeError("this Mac is already signed in")
    if claim_status()["running"]:
        raise RuntimeError("a sign-in is already waiting for its code")
    name = (name or "").strip() or device_name()
    status, data = call("POST", "/device/start",
                        {"name": name, "profile": config.PROFILE.name})
    if status != 200 or not data.get("device_code") or not data.get("user_code"):
        raise RuntimeError(f"the account service could not start a sign-in ({status})")
    expires_at = time.time() + int(data.get("expires_in") or DEFAULT_CODE_SECONDS)
    interval = int(data.get("interval") or DEFAULT_INTERVAL)
    _set_claim(running=True, user_code=data["user_code"],
               verification_uri=data.get("verification_uri", url() + "/device"),
               verification_uri_complete=data.get("verification_uri_complete", ""),
               expires_at=expires_at, error="")
    _say(f"sign-in started: enter {data['user_code']} at {url()}/device")
    if background:
        threading.Thread(target=run_claim, args=(data["device_code"], name, interval, expires_at),
                         daemon=True).start()
    return {"device_code": data["device_code"], "name": name, "interval": interval,
            **claim_status()}


def run_claim(device_code: str, name: str, interval: int, expires_at: float,
              sleep=time.sleep, now=time.time) -> Account | None:
    """Poll until the code is approved, expires, or is refused.

    Network trouble is not a refusal: the service may be briefly unreachable
    and the code is still good, so polling continues until it expires.
    """
    try:
        while now() < expires_at:
            sleep(interval)
            try:
                status, data = call("POST", "/device/poll", {"device_code": device_code})
            except Exception as exc:
                _say(f"could not reach the account service; trying again: {exc}")
                continue
            if status == 200 and data.get("token"):
                acct_data = data.get("account") or {}
                dev = data.get("device") or {}
                account = Account(
                    token=str(data["token"]),
                    account_id=str(acct_data.get("id", "")),
                    email=str(acct_data.get("email", "")),
                    name=str(acct_data.get("name", "")),
                    device_id=str(dev.get("id", "")),
                    device_name=str(dev.get("name") or name),
                    profile=str(dev.get("profile") or config.PROFILE.name),
                    url=url(),
                    claimed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                )
                save(account)
                _say(f"this Mac is now signed in as {account.email}")
                _set_claim(running=False, error="")
                return account
            error = str(data.get("error", ""))
            if error == "authorization_pending":
                interval = int(data.get("interval") or interval)
                continue
            if error == "expired_token":
                break
            if error == "slow_down":
                interval += 5
                continue
            _set_claim(running=False, error="the account service refused the code; try again")
            return None
        _set_claim(running=False, error="the code expired before it was entered; try again")
        return None
    except Exception as exc:  # never let the thread die silently
        _set_claim(running=False, error=f"{type(exc).__name__}: {exc}")
        return None


def cancel_claim() -> None:
    """Stop showing a code. The poll thread notices on its next pass."""
    _set_claim(running=False, expires_at=0.0, error="")
