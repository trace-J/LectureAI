"""Tests for sync.py: the schedule file against the account's copy.

A scripted fake stands in for the account service, with the same version
rules the real one has. Everything lands in a throwaway home; no network.
From the project root:

    .venv/bin/python test_sync.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import SAMPLE_SCHEDULE, fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported

from intake import account, config, sync  # noqa: E402


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    except Exception as exc:
        print(f"FAIL  {label}\n      unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"ok    {label}")
    return True


results = []
SERVICE = "https://accounts.test"

OTHER_SCHEDULE = SAMPLE_SCHEDULE.replace('{ day = "Fri", start = 12, course = "RELI-3304" },\n', "")
BROKEN_SCHEDULE = "classes = [ { day = 'Funday', start = 9, course = 'X' } ]\n"


class FakeService:
    """The /settings/schedule endpoint, with the real one's version rules."""

    def __init__(self):
        self.doc = None          # {"content", "updated_at", "updated_by"}
        self.calls = []
        self.clock = 1000        # seconds; versions are ISO stamps from this
        self.down = False

    def stamp(self):
        self.clock += 1
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.clock)) + ".000Z"

    def set_remote(self, content):
        self.doc = {"content": content, "updated_at": self.stamp(), "updated_by": "other-mac"}
        return self.doc["updated_at"]

    def __call__(self, method, url, headers, body, timeout):
        if self.down:
            raise ConnectionError("no route to host")
        assert headers.get("Authorization") == "Bearer syd_abc", headers
        path = url[len(SERVICE):]
        self.calls.append((method, path, body))
        assert path == "/settings/schedule", path
        if method == "GET":
            return (200, dict(self.doc)) if self.doc else (404, {"error": "not_found"})
        assert method == "PUT"
        expected = body.get("expected_updated_at")
        current = self.doc["updated_at"] if self.doc else ""
        if expected is not None and expected != current:
            return 409, {"error": "conflict", "current": dict(self.doc) if self.doc else None}
        self.doc = {"content": body["content"], "updated_at": self.stamp(), "updated_by": "this-mac"}
        return 200, dict(self.doc)


def reset(signed_in=True, local=SAMPLE_SCHEDULE):
    config.ACCOUNTS_URL = SERVICE
    account.forget()
    state = sync._state_file()
    if state.exists():
        state.unlink()
    for bak in config.SCHEDULE_FILE.parent.glob("schedule.toml.*.bak"):
        bak.unlink()
    if local is None:
        config.SCHEDULE_FILE.unlink(missing_ok=True)
    else:
        config.SCHEDULE_FILE.write_text(local)
    if signed_in:
        account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                     "syllabus", SERVICE, "x"))
    service = FakeService()
    account.transport = service
    return service


def local_text():
    return config.SCHEDULE_FILE.read_text() if config.SCHEDULE_FILE.exists() else None


def backups():
    return sorted(config.SCHEDULE_FILE.parent.glob("schedule.toml.*.bak"))


def t1():
    service = reset(signed_in=False)
    assert sync.sync() == "skipped" and service.calls == []
    assert sync.sync_later("start") is False
    config.ACCOUNTS_URL = "off"
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    assert sync.sync() == "skipped" and service.calls == [], "accounts off means no sync"
    assert sync.status()["synced_at"] == ""
results.append(run("nothing happens without an account, or with accounts turned off", t1))


def t2():
    service = reset()
    assert sync.sync("start") == "pushed"
    assert service.doc["content"] == SAMPLE_SCHEDULE
    assert service.calls[-1][2]["expected_updated_at"] == "", "a first push expects nothing stored"
    st = sync.status()
    assert st["outcome"] == "pushed" and st["synced_at"] and not st["error"], st
    # Nothing changed anywhere: one GET, no write.
    n = len(service.calls)
    assert sync.sync() == "same"
    assert service.calls[n:] == [("GET", "/settings/schedule", None)], service.calls[n:]
results.append(run("a Mac with a schedule and an account with none pushes, then stays quiet", t2))


def t3():
    service = reset(local=None)
    service.set_remote(OTHER_SCHEDULE)
    assert sync.sync("start") == "pulled"
    assert local_text() == OTHER_SCHEDULE
    assert config.courses() == ["ACCT-4321", "ENTR-3306", "ENTR-4306"], config.courses()
    assert sync.status()["outcome"] == "pulled"
results.append(run("a fresh Mac with no schedule pulls the account's copy and uses it at once", t3))


def t4():
    service = reset()
    assert sync.sync() == "pushed"
    # The Setup page saved a new schedule here.
    config.SCHEDULE_FILE.write_text(OTHER_SCHEDULE)
    assert sync.sync("save") == "pushed"
    assert service.doc["content"] == OTHER_SCHEDULE
    put = [c for c in service.calls if c[0] == "PUT"][-1]
    assert put[2]["expected_updated_at"], "a later push names the version it was built on"
    # Another Mac saved a schedule; this Mac has not touched its own.
    service.set_remote(SAMPLE_SCHEDULE)
    assert sync.sync("start") == "pulled"
    assert local_text() == SAMPLE_SCHEDULE and backups() == []
results.append(run("only this Mac changed: push; only the account changed: pull", t4))


def t5():
    service = reset()
    assert sync.sync() == "pushed"
    # Both changed since they last agreed. The account's copy is newer than the file.
    config.SCHEDULE_FILE.write_text(OTHER_SCHEDULE)
    mine = OTHER_SCHEDULE.replace("tolerance_minutes = 45", "tolerance_minutes = 30")
    remote_at = service.set_remote(mine)
    service.doc["updated_at"] = "2099-01-01T00:00:00.000Z"
    assert sync.sync() == "conflict-pulled"
    assert local_text() == mine, "the newer copy, the account's, wins"
    assert len(backups()) == 1 and backups()[0].read_text() == OTHER_SCHEDULE, "the loser is kept"

    # Both changed, and this Mac's file is the newer one.
    service = reset()
    assert sync.sync() == "pushed"
    service.set_remote(OTHER_SCHEDULE)
    service.doc["updated_at"] = "2000-01-01T00:00:00.000Z"
    config.SCHEDULE_FILE.write_text(mine)
    assert sync.sync() == "conflict-pushed"
    assert service.doc["content"] == mine and local_text() == mine
    assert backups() == []
results.append(run("both changed: the newer copy wins and the other is kept as a .bak", t5))


def t6():
    service = reset()
    assert sync.sync() == "pushed"
    # A broken schedule on the account never replaces a working file.
    service.set_remote(BROKEN_SCHEDULE)
    assert sync.sync() == "offline"
    assert local_text() == SAMPLE_SCHEDULE
    st = sync.status()
    assert "could not be read" in st["error"], st
    assert config.courses(), "the loaded schedule is untouched"
    # A good one afterward clears the error.
    service.set_remote(OTHER_SCHEDULE)
    assert sync.sync() == "pulled" and sync.status()["error"] == ""
results.append(run("a broken schedule on the account is refused and reported, not installed", t6))


def t7():
    service = reset()
    assert sync.sync() == "pushed"
    service.down = True
    config.SCHEDULE_FILE.write_text(OTHER_SCHEDULE)
    assert sync.sync("save") == "offline"
    st = sync.status()
    assert "ConnectionError" in st["error"] and st["outcome"] == "pushed", st
    assert st["tried_at"] >= st["synced_at"]
    assert local_text() == OTHER_SCHEDULE, "being offline changes nothing on this Mac"
    service.down = False
    assert sync.sync("start") == "pushed", "the change goes up once the service is back"
    assert sync.status()["error"] == ""
results.append(run("the service being down is reported and retried, and touches nothing locally", t7))


def t8():
    service = reset()
    assert sync.sync() == "pushed"
    config.SCHEDULE_FILE.write_text(OTHER_SCHEDULE)
    # Another Mac writes between our GET and our PUT: the service says 409.
    real_call = service.__call__

    def racy(method, url, headers, body, timeout):
        if method == "PUT" and not getattr(racy, "done", False):
            racy.done = True
            service.set_remote(SAMPLE_SCHEDULE.replace("45", "40"))
        return real_call(method, url, headers, body, timeout)
    account.transport = racy
    assert sync.sync("save") == "conflict-pulled"
    assert "40" in local_text() and len(backups()) == 1
results.append(run("a write that loses a race pulls the winner and keeps this Mac's copy", t8))


def t9():
    service = reset()
    # A Mac that never synced, with a schedule that already matches the account's.
    service.set_remote(SAMPLE_SCHEDULE)
    assert sync.sync() == "same" and backups() == []
    # sync_later throttles repeated Setup-page loads but not a save.
    sync._last_attempt = 0.0
    assert sync.sync_later("setup", throttle=True) is True
    assert sync.sync_later("setup", throttle=True) is False
    assert sync.sync_later("save") is True
    time.sleep(0.2)
results.append(run("matching copies agree without a write, and Setup-page loads are throttled", t9))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
