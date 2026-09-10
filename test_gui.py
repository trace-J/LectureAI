"""Tests for gui.py's log parsing, inbox listing, and API guard rails.

Uses Flask's test client against temporary directories: no server is started,
no microphone is opened, and no watcher is spawned. From the project root:

    .venv/bin/python test_gui.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config  # noqa: E402
from intake import gui  # noqa: E402


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


def t4b():
    """The sixth field says what did not reach Notion; five-field lines predate it."""
    log_file = tmp / "warned.log"
    warned = "\t".join([
        "2026-09-09T09:54:13", "ENTR-4306", "ENTR-4306_2026-09-09_0859.m4a",
        "ENTR-4306_2026-09-09_Exploitation-Vs-Exploration",
        "https://docs.google.com/document/d/CCC/edit",
        "2 of 3 to-dos did not reach Notion: no week heading covers 2026-09-11",
    ]) + "\n"
    log_file.write_text(LOG + warned)
    config.LOG_FILE = log_file
    rows = gui._recent()
    assert rows[0]["warning"].startswith("2 of 3 to-dos"), rows[0]
    assert rows[0]["name"].endswith("Exploitation-Vs-Exploration"), rows[0]
    assert rows[0]["error"] is None, rows[0]
    # Lines written before the field existed still read as clean runs.
    assert all(r["warning"] == "" for r in rows[1:]), rows[1:]
results.append(run("a lecture with dropped to-dos carries a warning", t4b))


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


def t12b():
    import json as _json
    from intake import watch
    status = tmp / "status.json"
    config.STATUS_FILE = status
    config.LOCK_FILE = tmp / "no-such.lock"

    watch.write_status("transcribing", "lecture.m4a", "ACCT-4321", "part 3 of 10")
    raw = _json.loads(status.read_text())
    assert raw["stage"] == "transcribing" and raw["detail"] == "part 3 of 10", raw

    # The panel only trusts a status stamped by the watcher that is running.
    seen = gui._processing(raw["pid"])
    assert seen["stage"] == "transcribing", seen
    assert seen["file"] == "lecture.m4a" and seen["course"] == "ACCT-4321", seen
    assert seen["elapsed"] >= 0, seen
results.append(run("the watcher's status is read back by the panel", t12b))


def t12c():
    from intake import watch
    config.STATUS_FILE = tmp / "status.json"
    watch.write_status("summarizing", "lecture.m4a")
    # A watcher that died mid-lecture leaves this behind; a stale stage shown
    # forever is worse than showing nothing.
    assert gui._processing(None) is None, "reported progress with no watcher"
    assert gui._processing(424242) is None, "reported another process's status"

    config.STATUS_FILE.write_text("{ truncated")
    import os as _os
    assert gui._processing(_os.getpid()) is None, "a corrupt status file must not raise"

    watch.clear_status()
    assert not config.STATUS_FILE.exists()
    assert gui._processing(_os.getpid()) is None
results.append(run("stale, foreign, or corrupt status reads as idle", t12c))


def t12d():
    import os as _os
    from intake import watch
    config.STATUS_FILE = tmp / "status.json"
    watch.write_status("uploading", "lecture.m4a", "ACCT-4321")
    body = client.get("/api/status").get_json()
    assert "processing" in body, "status payload is missing processing"
    # No watcher is running in the test, so it must report idle.
    assert body["processing"] is None, body["processing"]
    watch.clear_status()
results.append(run("the status payload carries a processing field", t12d))


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


def t14():
    html = client.get("/").get_data(as_text=True)
    for choice in ("system", "light", "dark"):
        assert f'data-theme-choice="{choice}"' in html, f"{choice} theme button missing"
    assert 'localStorage.getItem("lectureai-theme")' in html, "theme is not remembered"
results.append(run("the page offers system, light, and dark", t14))


def t15():
    html = client.get("/").get_data(as_text=True)
    # The saved theme must be applied before the stylesheet is parsed,
    # otherwise the page paints the OS theme first and visibly corrects.
    script = html.find("lectureai-theme")
    style = html.find("<style>")
    assert script != -1 and style != -1, "missing script or style"
    assert script < style, "theme script runs after the stylesheet; page will flash"
results.append(run("the saved theme is applied before first paint", t15))


def t16():
    html = client.get("/").get_data(as_text=True)
    # Both halves of every themed token live in one light-dark() call, so a
    # colour cannot be updated in one theme and forgotten in the other.
    assert "color-scheme: light dark" in html, "root does not follow the OS by default"
    for choice in ("light", "dark"):
        assert f':root[data-theme="{choice}"]' in html, f"no explicit {choice} override"
        assert f"color-scheme: {choice};" in html, f"{choice} does not set color-scheme"
    assert "light-dark(#faf9fc, #131019)" in html, "background is not theme-aware"
results.append(run("themes are driven by color-scheme, not a second palette", t16))


def t17():
    html = client.get("/").get_data(as_text=True)
    # .row sets display:flex, which outranks the browser's [hidden] rule, so
    # the idle processing row stayed on screen reading "Working" forever.
    assert "[hidden] { display: none !important; }" in html, \
        "hidden elements will stay visible"
results.append(run("[hidden] beats the display rules that broke it", t17))


# --- setup page -------------------------------------------------------------

setup_home = tmp / "setup-home"
setup_home.mkdir()


def point_config_at(home):
    config.HOME_DIR = home
    config.ENV_FILE = home / ".env"
    config.SCHEDULE_FILE = home / "schedule.toml"
    config.TOKEN_FILE = home / "token.json"
    config.INBOX_DIR = home / "inbox"
    config.PROCESSED_DIR = home / "processed"
    config.WORK_DIR = home / ".work"
    config.reload()


# No microphone is opened: the device list is canned, the way test_record does it.
gui.recording.list_devices = lambda: [(0, "Someone's iPhone Microphone"),
                                      (1, "MacBook Pro Microphone")]


def t18():
    res = client.get("/setup")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Lexend Deca" in html and "#8C52FF" in html, "setup page is off-brand"
    for step in ("API keys", "Microphone", "Class schedule", "Notion", "Google Drive"):
        assert step in html, f"setup page is missing {step}"
    assert "/api/setup" in html and "/api/drive/login" in html
    # The panel itself must offer a way in.
    assert 'href="/setup"' in client.get("/").get_data(as_text=True)
results.append(run("the setup page renders with every step and the panel links to it", t18))


def t19():
    point_config_at(setup_home)
    # An empty home: no schedule, no keys. The panel must still answer.
    body = client.get("/api/status").get_json()
    assert body["courses"] == [] and body["configured"] is False, body
    assert body["now_class"] is None
    state = client.get("/api/setup").get_json()
    assert state["openai_set"] is False and state["schedule"] == [], state
    assert [d["name"] for d in state["devices"]] == ["Someone's iPhone Microphone",
                                                     "MacBook Pro Microphone"]
    assert state["drive"]["connected"] is False
    assert state["configured"] is False
results.append(run("with nothing configured the panel answers instead of failing", t19))


def t20():
    point_config_at(setup_home)
    res = client.post("/api/setup", json={
        "openai_key": "sk-openai-test-key-000000",
        "anthropic_key": "sk-ant-test-key-0000000000",
        "device": "MacBook Pro Microphone",
        "schedule": [{"day": "Tue", "start": 14, "course": "acct-4321"},
                     {"day": "Thursday", "start": "14", "course": "ACCT-4321"}],
        "tolerance": 30,
        "notion": {"enabled": False},
    })
    assert res.status_code == 200, res.get_json()
    out = res.get_json()
    assert out["ok"] and out["configured"] and out["classes"] == 2, out

    from intake import setup_wizard
    env = setup_wizard.read_env(setup_home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-openai-test-key-000000", env
    assert env["RECORD_DEVICE"] == "MacBook Pro Microphone", env
    assert not env.get("NOTION_TOKEN"), env
    loaded = config.load_schedule(setup_home / "schedule.toml")
    assert loaded.by_slot == {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"}, loaded.by_slot
    assert loaded.tolerance_minutes == 30
    # The running panel picks the new settings up without a restart.
    assert config.OPENAI_API_KEY == "sk-openai-test-key-000000"
    assert client.get("/api/status").get_json()["courses"] == ["ACCT-4321"]
    assert not (config.PACKAGE_DIR / ".env").exists()
results.append(run("saving the form writes .env and schedule.toml and reloads them", t20))


def t21():
    point_config_at(setup_home)
    state = client.get("/api/setup").get_json()
    # Masked, never the key itself.
    assert state["openai_set"] is True
    assert "sk-openai-test-key-000000" not in json.dumps(state), "key leaked to the page"
    assert state["openai"].startswith("sk-ope") and "..." in state["openai"], state["openai"]
    assert state["schedule"][0] == {"day": "Tue", "start": 14, "course": "ACCT-4321"}

    # Blank keys keep what is on file; one bad row is named.
    res = client.post("/api/setup", json={
        "openai_key": "", "anthropic_key": "", "device": "",
        "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"},
                     {"day": "Someday", "start": 9, "course": "X"}],
        "notion": {"enabled": False},
    })
    assert res.status_code == 400, res.get_json()
    assert res.get_json()["row"] == 2 and "row 2" in res.get_json()["error"]
    assert config.load_schedule(setup_home / "schedule.toml").by_slot == \
        {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"}, "a rejected save changed the file"

    res = client.post("/api/setup", json={
        "openai_key": "", "anthropic_key": "", "device": "",
        "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
        "notion": {"enabled": True, "token": "ntn_x", "database": ""},
    })
    assert res.status_code == 400 and "Notion" in res.get_json()["error"], res.get_json()

    res = client.post("/api/setup", json={
        "openai_key": "", "anthropic_key": "", "device": "",
        "schedule": [{"day": "Mon", "start": 9, "course": "ENTR-4306"}],
        "notion": {"enabled": False},
    })
    assert res.status_code == 200, res.get_json()
    from intake import setup_wizard
    env = setup_wizard.read_env(setup_home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-openai-test-key-000000", "blank key wiped the saved one"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-key-0000000000"
results.append(run("the page shows masked keys, keeps them on blank, and names a bad row", t21))


def t22():
    point_config_at(setup_home)
    res = client.post("/api/setup", json={"openai_key": "", "anthropic_key": "",
                                          "schedule": [], "notion": {"enabled": False}})
    assert res.status_code == 400 and "at least one class" in res.get_json()["error"]
    empty = tmp / "empty-home"; empty.mkdir()
    point_config_at(empty)
    res = client.post("/api/setup", json={"openai_key": "sk-only-one", "anthropic_key": "",
                                          "schedule": [{"day": "Mon", "start": 9, "course": "X-1"}],
                                          "notion": {"enabled": False}})
    assert res.status_code == 400 and "both API keys" in res.get_json()["error"]
    assert not (empty / ".env").exists(), "a rejected save must write nothing"
    point_config_at(setup_home)
results.append(run("no schedule or a missing key is refused before anything is written", t22))


def t23():
    point_config_at(setup_home)
    body = client.get("/api/doctor").get_json()
    names = [c["name"] for c in body["checks"]]
    for want in ("Python", "ffmpeg", "OpenAI key", "class schedule", "Drive authorization", "Notion"):
        assert want in names, f"doctor is missing {want}"
    assert "sk-openai-test-key-000000" not in json.dumps(body), "doctor leaked a key"
    drive = next(c for c in body["checks"] if c["name"] == "Drive authorization")
    assert drive["ok"] is False and drive["fix"] == "intake login"
    assert body["healthy"] is False
    assert "ok" not in body, "an ok field would read as a failed request on the page"
results.append(run("the checkup endpoint mirrors doctor without exposing secrets", t23))


def t24():
    point_config_at(setup_home)
    (setup_home / "token.json").write_text("{}")
    res = client.post("/api/drive/login", json={})
    assert res.status_code == 409, "must not start a login while a token exists"
    assert client.get("/api/setup").get_json()["drive"]["connected"] is True
    res = client.post("/api/drive/disconnect", json={})
    assert res.status_code == 200
    assert not (setup_home / "token.json").exists()
    assert client.get("/api/setup").get_json()["drive"]["connected"] is False
results.append(run("Drive login refuses to double up and disconnect removes the token", t24))


def t25():
    # Flask's run() reads a .env from the current directory by default, which
    # let a checkout's .env override the home directory's settings. The panel
    # must start with that off, on localhost, and without a schedule.
    captured = {}
    real_run, real_open = gui.app.run, gui._open_browser_later
    gui.app.run = lambda **kw: captured.update(kw)
    gui._open_browser_later = lambda url, delay=0: captured.update(opened=url)
    try:
        point_config_at(tmp / "another-empty-home")
        assert gui.main(["--no-browser", "--port", "5555"]) == 0
        assert captured["load_dotenv"] is False, captured
        assert captured["host"] == "127.0.0.1" and captured["port"] == 5555, captured
        assert "opened" not in captured, "--no-browser still opened a browser"
        captured.clear()
        assert gui.main(["--port", "5556"]) == 0
        assert captured["opened"].endswith(":5556/setup"), "an empty home must land on setup"
    finally:
        gui.app.run, gui._open_browser_later = real_run, real_open
        point_config_at(setup_home)
results.append(run("the panel starts without Flask's cwd .env, on localhost, landing on setup", t25))


def t26():
    # The panel that started a recording was closed; ffmpeg kept going. A new
    # panel must show that recording as live and be able to stop it, instead
    # of saying "Not recording" while the microphone is still open.
    import subprocess
    from datetime import datetime as _dt
    rec_mod = gui.recording
    state_file = config.RECORDING_STATE_FILE
    staging = config.WORK_DIR / "recording_20260910-123340.m4a"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(b"x" * 500)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    rec_mod._write_state(proc.pid, staging, _dt(2026, 9, 10, 12, 33, 40),
                         "ENTR-3306", "MacBook Pro Microphone")
    real_owner = rec_mod._process_is_recording
    rec_mod._process_is_recording = lambda pid, path: pid == proc.pid
    gui._recorder = None
    try:
        body = client.get("/api/status").get_json()
        r = body["recording"]
        assert r["active"] is True, r
        assert r["resumed"] is True, r
        assert r["planned"] == "ENTR-3306_2026-09-10_1233.m4a", r
        assert r["bytes"] == 500 and r["elapsed"] > 60, r
        # Starting another one is refused while it runs.
        res = client.post("/api/record/start", json={})
        assert res.status_code == 409, res.status_code
        res = client.post("/api/record/stop", json={})
        assert res.status_code == 200, (res.status_code, res.get_json())
        out = res.get_json()
        assert out["name"] == "ENTR-3306_2026-09-10_1233.m4a", out
        assert (config.INBOX_DIR / out["name"]).exists()
        assert not state_file.exists() and not staging.exists()
        assert gui._recorder is None
        assert client.get("/api/status").get_json()["recording"]["active"] is False
    finally:
        rec_mod._process_is_recording = real_owner
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        state_file.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)
        gui._recorder = None
results.append(run("a recording an earlier panel started is shown and can be stopped", t26))


def t27():
    # ffmpeg hit its time cap and exited while the panel was open. The next
    # status poll files the finished recording instead of leaving it in .work/.
    from datetime import datetime as _dt
    rec_mod = gui.recording
    staging = config.WORK_DIR / "recording_20260910-140000.m4a"
    staging.write_bytes(b"x" * 500)
    done = rec_mod.Recorder(course="ACCT-4321")
    done.started = _dt(2026, 9, 10, 14, 0, 0)
    done._staging = staging
    done._proc = rec_mod._ExternalProcess(2 ** 22 - 1)  # already gone
    gui._recorder = done
    body = client.get("/api/status").get_json()
    assert body["recording"]["active"] is False, body["recording"]
    assert gui._recorder is None
    filed = config.INBOX_DIR / "ACCT-4321_2026-09-10_1400.m4a"
    assert filed.exists(), sorted(p.name for p in config.INBOX_DIR.iterdir())
    filed.unlink()
results.append(run("a recording that ended on its own is filed on the next poll", t27))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
