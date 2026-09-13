"""Keep this Mac's schedule file in step with the account's copy.

Once a Mac is signed in to a Syllabus account (account.py), the class
schedule belongs to the account as well as to the file: the panel pushes
schedule.toml to the account service whenever the Setup page saves it, and
pulls the account's copy when the panel starts and that copy is newer.
Someone who sets up a second Mac and signs it in gets their schedule back
without retyping it.

The file on this Mac stays what the pipeline reads. The service holds the
text as written, comments and all, with a version (its updated_at). A small
state file in .work remembers the version and the local text this Mac last
agreed with the service on, which is how a change on either side is told
apart from no change at all:

    neither changed        nothing to do
    only this Mac changed  push
    only the account did   pull
    both changed           the newer copy wins; the loser is kept beside
                           the schedule file as schedule.toml.<stamp>.bak

A copy from the service is parsed before it replaces the file, so a broken
schedule on the account cannot break this Mac. Nothing here runs unless the
Mac is signed in, and the network never holds the panel up: everything is
done on a thread and reported on the Setup page.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone

from intake import account, config

DOC = "schedule"
# The Setup page loads often; do not hit the service more than this.
MIN_SECONDS_BETWEEN = 60


class Conflict(Exception):
    """The service refused a push because another Mac wrote in between."""

    def __init__(self, current: dict | None):
        super().__init__("another Mac wrote the schedule in between")
        self.current = current


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _state_file():
    return config.WORK_DIR / "sync.json"


def _read_state() -> dict:
    try:
        data = json.loads(_state_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local() -> tuple[str | None, datetime | None]:
    path = config.SCHEDULE_FILE
    if not path.exists():
        return None, None
    return path.read_text(), datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


# --- Talking to the service ----------------------------------------------------

def fetch(acct: account.Account) -> dict | None:
    """The account's copy, or None when it has none. Raises on trouble."""
    status, data = account.call("GET", f"/settings/{DOC}", token=acct.token)
    if status == 404:
        return None
    if status != 200 or not isinstance(data.get("content"), str):
        raise RuntimeError(f"the account service answered {status}")
    return data


def push(acct: account.Account, content: str, expected: str | None) -> dict:
    """Store this Mac's copy. `expected` is the version it was built on; ""
    means the account should have none yet; None means take it regardless."""
    body = {"content": content}
    if expected is not None:
        body["expected_updated_at"] = expected
    status, data = account.call("PUT", f"/settings/{DOC}", body, token=acct.token)
    if status == 409:
        raise Conflict(data.get("current"))
    if status != 200 or not data.get("updated_at"):
        raise RuntimeError(f"the account service answered {status}")
    return data


# --- The sync itself -----------------------------------------------------------

def _remember(updated_at: str, content: str, outcome: str) -> None:
    state = _read_state()
    state[DOC] = {"updated_at": updated_at, "sha": _sha(content),
                  "synced_at": _iso(datetime.now(timezone.utc)), "outcome": outcome,
                  "error": ""}
    _write_state(state)


def _remember_error(message: str) -> None:
    state = _read_state()
    entry = dict(state.get(DOC) or {})
    entry.update({"error": message, "tried_at": _iso(datetime.now(timezone.utc))})
    state[DOC] = entry
    _write_state(state)


def _install(content: str, source: str) -> bool:
    """Replace the schedule file with the account's copy, after checking it parses."""
    try:
        config.parse_schedule(content, source=f"the account's {config.SCHEDULE_FILE.name}")
    except config.ScheduleError as exc:
        _say(f"not pulling the schedule from the account: {exc}")
        _remember_error(f"the account's copy could not be read: {exc}")
        return False
    config.ensure_home()
    config.SCHEDULE_FILE.write_text(content)
    config.reload_schedule()
    _say(f"pulled the schedule from the account ({source})")
    return True


def _backup_local(text: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.SCHEDULE_FILE.with_name(f"{config.SCHEDULE_FILE.name}.{stamp}.bak")
    path.write_text(text)
    _say(f"kept this Mac's schedule as {path.name}")


def sync(reason: str = "start") -> str:
    """Bring the file and the account's copy into agreement.

    Returns one word saying what happened: skipped, same, pushed, pulled,
    conflict-pushed, conflict-pulled, or offline. Never raises.
    """
    acct = account.load() if account.enabled() else None
    if acct is None:
        return "skipped"
    local_text, local_mtime = _local()
    last = (_read_state().get(DOC) or {})
    try:
        remote = fetch(acct)
    except Exception as exc:
        _say(f"could not reach the account service to sync the schedule ({reason}): {exc}")
        _remember_error(f"{type(exc).__name__}: {exc}")
        return "offline"

    try:
        if remote is None:
            if local_text is None:
                return "same"
            stored = push(acct, local_text, expected="")
            _remember(stored["updated_at"], local_text, "pushed")
            _say(f"pushed the schedule to the account ({reason})")
            return "pushed"

        if local_text is None:
            if _install(remote["content"], reason):
                _remember(remote["updated_at"], remote["content"], "pulled")
                return "pulled"
            return "offline"

        if _sha(local_text) == _sha(remote["content"]):
            _remember(remote["updated_at"], local_text, "same")
            return "same"

        local_changed = _sha(local_text) != last.get("sha")
        remote_changed = remote["updated_at"] != last.get("updated_at")
        if not last:
            local_changed = remote_changed = True

        if local_changed and not remote_changed:
            stored = push(acct, local_text, expected=remote["updated_at"])
            _remember(stored["updated_at"], local_text, "pushed")
            _say(f"pushed the schedule to the account ({reason})")
            return "pushed"

        if remote_changed and not local_changed:
            if _install(remote["content"], reason):
                _remember(remote["updated_at"], remote["content"], "pulled")
                return "pulled"
            return "offline"

        # Both sides changed since this Mac last agreed with the account.
        if local_mtime is not None and local_mtime > _parse_time(remote["updated_at"]):
            stored = push(acct, local_text, expected=None)
            _remember(stored["updated_at"], local_text, "conflict-pushed")
            _say("both this Mac and the account changed the schedule; this Mac's is "
                 "newer and was pushed")
            return "conflict-pushed"
        _backup_local(local_text)
        if _install(remote["content"], "the account's copy is newer"):
            _remember(remote["updated_at"], remote["content"], "conflict-pulled")
            return "conflict-pulled"
        return "offline"
    except Conflict as exc:
        # Someone else wrote while we were deciding: their copy is the newer one.
        current = exc.current
        if current and isinstance(current.get("content"), str) and local_text is not None:
            _backup_local(local_text)
            if _install(current["content"], "another Mac wrote it first"):
                _remember(current["updated_at"], current["content"], "conflict-pulled")
                return "conflict-pulled"
        _remember_error(str(exc))
        return "offline"
    except Exception as exc:
        _say(f"could not sync the schedule ({reason}): {exc}")
        _remember_error(f"{type(exc).__name__}: {exc}")
        return "offline"


# --- Running it without holding anything up --------------------------------------

_lock = threading.Lock()
_last_attempt = 0.0


def sync_later(reason: str, throttle: bool = False) -> bool:
    """Run sync on a thread. With throttle, skip if one ran recently."""
    global _last_attempt
    with _lock:
        if throttle and time.monotonic() - _last_attempt < MIN_SECONDS_BETWEEN:
            return False
        _last_attempt = time.monotonic()
    if not (account.enabled() and account.load() is not None):
        return False
    threading.Thread(target=sync, args=(reason,), daemon=True).start()
    return True


def status() -> dict:
    """For the Setup page: what the last sync did and when, no network."""
    entry = _read_state().get(DOC) or {}
    return {"synced_at": entry.get("synced_at", ""), "outcome": entry.get("outcome", ""),
            "error": entry.get("error", ""), "tried_at": entry.get("tried_at", "")}
