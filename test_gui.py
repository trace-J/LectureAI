"""Tests for gui.py's log parsing, inbox listing, and API guard rails.

Uses Flask's test client against temporary directories: no server is started,
no microphone is opened, and no watcher is spawned. From the project root:

    .venv/bin/python test_gui.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import gui  # noqa: E402


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


tmp = Path(tempfile.mkdtemp())
results = []

# --- pipeline.log parsing -------------------------------------------------

# Real lines from pipeline.log: five fields on success, four on failure.
LOG = "\t".join(["2026-09-02T10:31:15", "ENTR-4306", "Dallas Baptist University 8.m4a",
                 "ENTR-4306_2026-09-02_Theories-Of-Leadership",
                 "https://docs.google.com/document/d/AAA/edit"]) + "\n" + \
      "\t".join(["2026-09-01T16:20:14", "ERROR", "corrupt-recording.m4a",
                 "Audio file might be corrupted or unsupported"]) + "\n" + \
      "\t".join(["2026-09-08T13:57:17", "ENTR-3306", "Dallas Baptist University 11.m4a",
                 "ENTR-3306_2026-09-08_Technology-And-Brain-Drain",
                 "https://docs.google.com/document/d/BBB/edit"]) + "\n"


def t1():
    log_file = tmp / "pipeline.log"
    log_file.write_text(LOG)
    config.LOG_FILE = log_file
    rows = gui._recent()
    assert len(rows) == 3, rows
    # Newest first, so the last line written comes back first.
    assert rows[0]["name"] == "ENTR-3306_2026-09-08_Technology-And-Brain-Drain", rows[0]
    assert rows[0]["url"].endswith("BBB/edit"), rows[0]
    assert rows[0]["error"] is None
results.append(run("parses successful lectures, newest first", t1))


def t2():
    config.LOG_FILE = tmp / "pipeline.log"
    err = [r for r in gui._recent() if r["error"]]
    assert len(err) == 1, err
    assert err[0]["course"] == "ERROR", err[0]
    assert "corrupted" in err[0]["error"], err[0]
    assert err[0]["source"] == "corrupt-recording.m4a", err[0]
results.append(run("a four-field error line is read as a failure", t2))


def t3():
    config.LOG_FILE = tmp / "pipeline.log"
    assert len(gui._recent(limit=2)) == 2
results.append(run("the recent list respects its limit", t3))


def t4():
    config.LOG_FILE = tmp / "does-not-exist.log"
    assert gui._recent() == []
    # A truncated or half-written line must not take the panel down.
    broken = tmp / "broken.log"
    broken.write_text("garbage\nalso\tgarbage\n\n")
    config.LOG_FILE = broken
    assert gui._recent() == []
results.append(run("a missing or malformed log yields nothing, not an error", t4))


# --- inbox ----------------------------------------------------------------

def t5():
    inbox = tmp / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "lecture.m4a").write_bytes(b"x" * 2048)
    (inbox / "notes.txt").write_text("ignored")
    (inbox / ".DS_Store").write_bytes(b"ignored")
    (inbox / ".hidden.m4a").write_bytes(b"ignored")
    config.INBOX_DIR = inbox
    items = gui._inbox()
    assert [i["name"] for i in items] == ["lecture.m4a"], items
    assert items[0]["bytes"] == 2048, items
results.append(run("the inbox lists audio only, skipping dotfiles", t5))


# --- integrations ---------------------------------------------------------

def t6():
    config.OPENAI_API_KEY = "sk-test"
    config.ANTHROPIC_API_KEY = ""
    config.TOKEN_FILE = tmp / "missing-token.json"
    config.NOTION_TOKEN = ""
    config.NOTION_DATABASE = ""
    checks = gui._integrations()
    assert checks == {"openai": True, "anthropic": False,
                      "drive": False, "notion": False}, checks
results.append(run("setup checks report what is configured", t6))


def t7():
    # Presence only. A key's value must never reach the browser.
    config.OPENAI_API_KEY = "sk-supersecret-value"
    assert all(isinstance(v, bool) for v in gui._integrations().values())
    assert "supersecret" not in repr(gui._integrations())
results.append(run("setup checks expose no secret values", t7))


# --- API guard rails ------------------------------------------------------

client = gui.app.test_client()


def t8():
    gui._recorder = None
    res = client.post("/api/record/stop", json={})
    assert res.status_code == 409, res.status_code
    assert res.get_json()["ok"] is False
results.append(run("stopping when not recording is a clean 409", t8))


def t9():
    gui._recorder = None
    res = client.post("/api/record/start", json={"course": "NOPE-0000"})
    assert res.status_code == 400, res.status_code
    assert "NOPE-0000" in res.get_json()["error"]
    # An invalid course must not leave a half-built recorder behind.
    assert gui._recorder is None
results.append(run("an unknown course is rejected before the mic opens", t9))


def t10():
    config.LOCK_FILE = tmp / "no-such.lock"
    res = client.post("/api/watcher/stop", json={})
    assert res.status_code == 409, res.status_code
results.append(run("stopping a watcher that isn't running is a clean 409", t10))


def t11():
    config.LOCK_FILE = tmp / "stale.lock"
    # A PID that no longer exists must read as "not running", not as running.
    config.LOCK_FILE.write_text("999999")
    assert gui._watcher_pid() is None
    config.LOCK_FILE.write_text("not-a-pid")
    assert gui._watcher_pid() is None
    config.LOCK_FILE = tmp / "no-such.lock"
    assert gui._watcher_pid() is None
results.append(run("a stale or corrupt lock file reads as stopped", t11))


def t12():
    config.LOG_FILE = tmp / "pipeline.log"
    config.INBOX_DIR = tmp / "inbox"
    config.LOCK_FILE = tmp / "no-such.lock"
    gui._recorder = None
    body = client.get("/api/status").get_json()
    for key in ("recording", "watcher", "inbox", "recent", "integrations",
                "courses", "now_class"):
        assert key in body, f"status is missing {key}"
    assert body["recording"]["active"] is False
    assert body["watcher"]["running"] is False
    assert isinstance(body["courses"], list) and body["courses"]
results.append(run("the status payload has everything the page reads", t12))


def t13():
    res = client.get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    # The branding has to actually render, not just exist in Assets.
    assert "Lexend Deca" in html, "brand font missing"
    assert "#8C52FF" in html, "brand purple missing"
    assert "/static/icon.png" in html, "logo missing"
    assert "All of your lectures, in one place" in html, "tagline missing"
results.append(run("the page renders with the brand font, color, and logo", t13))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
