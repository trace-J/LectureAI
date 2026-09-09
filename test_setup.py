"""Tests for the home directory resolver, the schedule file, and `lectureai setup`.

Everything runs against throwaway directories with scripted answers: no
network, no keys, no microphone, and nothing under ~/.lectureai or in this
checkout is read or written. From the project root:

    .venv/bin/python test_setup.py
"""
import os
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import SAMPLE_SCHEDULE, fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported

from lectureai import cli, config, doctor, setup_wizard  # noqa: E402

# The migration check looks for an older install next to the code, and during
# tests "the code" is this checkout, which really does have one. Point it at
# an empty fake checkout so no test can ever offer to move the real files.
FAKE_CODE_ROOT = Path(tempfile.mkdtemp(prefix="lectureai-fake-checkout-"))
(FAKE_CODE_ROOT / "pyproject.toml").write_text("[project]\nname = 'x'\n")
config.CODE_ROOT = FAKE_CODE_ROOT


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

# --- home directory --------------------------------------------------------

def t1():
    got = config.resolve_home({"LECTUREAI_HOME": "/tmp/somewhere"})
    assert got == Path("/tmp/somewhere").resolve(), got
    got = config.resolve_home({"LECTUREAI_HOME": "~/lectures"})
    assert got == (Path.home() / "lectures").resolve(), got
results.append(run("$LECTUREAI_HOME wins, with ~ expanded", t1))


def t2():
    for env in ({}, {"LECTUREAI_HOME": ""}, {"LECTUREAI_HOME": "   "}):
        got = config.resolve_home(env)
        assert got == (Path.home() / ".lectureai").resolve(), (env, got)
results.append(run("unset or blank falls back to ~/.lectureai", t2))


def t3():
    assert config.HOME_DIR == HOME.resolve(), config.HOME_DIR
    for path in (config.INBOX_DIR, config.PROCESSED_DIR, config.WORK_DIR,
                 config.LOG_FILE, config.LOCK_FILE, config.TOKEN_FILE,
                 config.CREDENTIALS_FILE, config.DRIVE_ROOT_CACHE,
                 config.SCHEDULE_FILE, config.ENV_FILE, config.STATUS_FILE):
        assert HOME.resolve() in path.parents, f"{path} is not under the home"
        assert config.PACKAGE_DIR not in path.parents, f"{path} is under the code"
results.append(run("every data path lives under the home, none under the code", t3))


def t4():
    for sub in ("inbox", "processed", ".work"):
        assert (HOME / sub).is_dir(), f"{sub} was not created"
    assert not (Path.home() / ".lectureai").exists() or \
        os.environ.get("LECTUREAI_HOME"), "tests must not create the real home"
results.append(run("importing config creates the home's subfolders", t4))


# --- schedule file ---------------------------------------------------------

def t5():
    loaded = config.parse_schedule(SAMPLE_SCHEDULE)
    assert len(loaded.meetings) == 8, loaded
    assert loaded.by_slot[("Tue", 14)] == "ACCT-4321", loaded.by_slot
    assert loaded.courses() == ["ACCT-4321", "ENTR-3306", "ENTR-4306", "RELI-3304"]
    assert loaded.tolerance_minutes == 45
results.append(run("a valid schedule file loads every row", t5))


def t6():
    text = 'classes = [ { day = "Mon", start = 9, course = "X-1000" } ]\n'
    loaded = config.parse_schedule(text)
    assert loaded.tolerance_minutes == config.DEFAULT_TOLERANCE_MINUTES == 45, loaded
results.append(run("tolerance defaults to 45 when the file leaves it out", t6))


def t7():
    text = ('classes = [ { day = "Mon", start = 9, course = "X-1000" } ]\n'
            'tolerance_minutes = 20\n')
    assert config.parse_schedule(text).tolerance_minutes == 20
    for bad in ("tolerance_minutes = -5", 'tolerance_minutes = "45"',
                "tolerance_minutes = true"):
        try:
            config.parse_schedule(f"classes = []\n{bad}\n")
        except config.ScheduleError as exc:
            assert "tolerance_minutes" in str(exc), exc
        else:
            raise AssertionError(f"{bad!r} should have been rejected")
results.append(run("tolerance is read from the file and type-checked", t7))


def t8():
    missing = HOME / "nope" / "schedule.toml"
    try:
        config.load_schedule(missing)
    except config.ScheduleError as exc:
        assert "lectureai setup" in str(exc), exc
        assert str(missing) in str(exc), exc
    else:
        raise AssertionError("a missing file should raise ScheduleError")
    assert issubclass(config.ScheduleError, RuntimeError), \
        "the module mains catch RuntimeError, so ScheduleError must be one"
results.append(run("a missing schedule says so and points at lectureai setup", t8))


def t9():
    cases = {
        'classes = [ { day = "Funday", start = 9, course = "X-1000" } ]': "row 1",
        'classes = [ { day = "Mon", start = 9, course = "X" },\n'
        '            { day = "Tue", start = 25, course = "X" } ]': "row 2",
        'classes = [ { day = "Mon", course = "X-1000" } ]': "missing start",
        'classes = [ { day = "Mon", start = 9, course = "" } ]': "empty",
        'classes = [ "Mon 9 X-1000" ]': "row 1",
        'classes = [ { day = "Mon", start = "9:30", course = "X" } ]': "whole hour",
        'tolerance_minutes = 45': "classes",
        'this is not toml': "not valid TOML",
    }
    for text, expect in cases.items():
        try:
            config.parse_schedule(text)
        except config.ScheduleError as exc:
            assert expect in str(exc), f"{text!r}: {exc}"
        else:
            raise AssertionError(f"{text!r} should have been rejected")
results.append(run("a malformed row is rejected naming the row and the problem", t9))


def t10():
    loaded = config.parse_schedule(
        'classes = [ { day = "tuesday", start = "2pm", course = " ACCT-4321 " },\n'
        '            { day = "Thur", start = "14:00", course = "ACCT-4321" } ]')
    assert loaded.by_slot == {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321"}, \
        loaded.by_slot
results.append(run("day and hour spellings people actually type are accepted", t10))


def t11():
    text = config.render_schedule(
        [("Thu", 14, "ACCT-4321"), ("Mon", 9, "ENTR-4306"), ("Mon", 12, "RELI-3304")],
        tolerance_minutes=30)
    back = config.parse_schedule(text)
    assert back.by_slot == {("Mon", 9): "ENTR-4306", ("Mon", 12): "RELI-3304",
                            ("Thu", 14): "ACCT-4321"}, back.by_slot
    assert back.tolerance_minutes == 30
    # The reason the tolerance must stay small travels with the file.
    assert "back-to-back" in text and "tolerance_minutes = 30" in text
    lines = [l for l in text.splitlines() if l.strip().startswith("{")]
    assert len(lines) == 3, "one row per meeting"
results.append(run("a written schedule reads back identically, with its comment", t11))


def t12():
    # The pipeline's matching reads the loaded file, not a constant.
    path = HOME / "alt-schedule.toml"
    config.write_schedule([("Sun", 3, "TEST-0001")], path=path)
    config.set_schedule(config.load_schedule(path))
    try:
        assert config.infer_course(datetime(2026, 9, 6, 3, 10)) == "TEST-0001"
        assert config.infer_course_from_filename("x-test-0001.m4a") == "TEST-0001"
        assert config.infer_course_from_filename("x-acct-4321.m4a") == config.UNKNOWN_COURSE
        assert config.next_class_meeting("TEST-0001", "2026-09-06") == "2026-09-13"
        assert config.courses() == ["TEST-0001"]
        assert config.SCHEDULE == {("Sun", 3): "TEST-0001"}
        assert config.SCHEDULE_TOLERANCE_MINUTES == 45
    finally:
        config.set_schedule(None)
    assert config.infer_course(datetime(2026, 9, 8, 14, 0)) == "ACCT-4321"
results.append(run("course inference, filename fallback, and next meeting follow the file", t12))


# --- setup wizard ------------------------------------------------------------

class Script:
    """Scripted answers for the wizard, in order. Records what was printed."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.said = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"wizard asked more than expected: {prompt!r}")
        return self.answers.pop(0)

    def say(self, text):
        self.said.append(text)

    def output(self):
        return "\n".join(self.said)


DEVICES = [(0, "Someone's iPhone Microphone"), (1, "MacBook Pro Microphone"),
           (2, "USB Headset")]


def wizard(home, answers, devices=DEVICES):
    script = Script(answers)
    w = setup_wizard.Wizard(ask=script.ask, say=script.say, home=home,
                            devices=devices, allow_login=False)
    return w, script


def t13():
    home = Path(tempfile.mkdtemp())
    w, script = wizard(home, [
        "sk-openai-test-key-000000",   # OpenAI
        "sk-ant-test-key-0000000000",  # Anthropic
        "2",                           # microphone by number
        "Tue 14 ACCT-4321",            # schedule rows
        "thu 2pm acct-4321",
        "Mon 9 ENTR-4306",
        "",                            # blank line ends the schedule
        "n",                           # no Notion
    ])
    assert w.run() == 0
    assert script.answers == [], f"unused answers: {script.answers}"

    env = setup_wizard.read_env(home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-openai-test-key-000000", env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-key-0000000000", env
    assert env["RECORD_DEVICE"] == "USB Headset", env
    assert not env.get("NOTION_TOKEN") and not env.get("NOTION_DATABASE"), env
    assert setup_wizard.NOTION_SKIP_MARKER in (home / ".env").read_text()

    loaded = config.load_schedule(home / "schedule.toml")
    assert loaded.by_slot == {("Tue", 14): "ACCT-4321", ("Thu", 14): "ACCT-4321",
                              ("Mon", 9): "ENTR-4306"}, loaded.by_slot
    for sub in ("inbox", "processed", ".work"):
        assert (home / sub).is_dir(), sub
results.append(run("first run writes .env and schedule.toml into the home", t13))


def t14():
    home = Path(tempfile.mkdtemp())
    w, _ = wizard(home, ["k1", "k2", "1", "Tue 14 ACCT-4321", "", "n"])
    assert w.run() == 0
    mode = stat.S_IMODE((home / ".env").stat().st_mode)
    assert mode == 0o600, oct(mode)
    written = {p.name for p in home.iterdir()}
    assert written == {".env", "schedule.toml", "inbox", "processed", ".work"}, written
    assert not any(FAKE_CODE_ROOT.iterdir().__next__().name == ".env"
                   for _ in [0]), "wrote into the code directory"
    assert not (config.PACKAGE_DIR / ".env").exists()
    assert not (config.PACKAGE_DIR / "schedule.toml").exists()
results.append(run(".env is private to the user and nothing lands in the code dir", t14))


def t15():
    home = Path(tempfile.mkdtemp())
    w, _ = wizard(home, ["sk-first-key-0000000000", "sk-ant-first-000000000", "1",
                         "Tue 14 ACCT-4321", "Fri 12 RELI-3304", "", "n"])
    assert w.run() == 0
    (home / ".env").write_text((home / ".env").read_text()
                               + "\nDRIVE_PARENT_FOLDER_ID=folder123\n")

    # Second run: change only the OpenAI key, keep everything else with Enter.
    w, script = wizard(home, ["sk-second-key-000000000", "", "", "", "n"])
    assert w.run() == 0
    assert script.answers == [], script.answers
    env = setup_wizard.read_env(home / ".env")
    assert env["OPENAI_API_KEY"] == "sk-second-key-000000000", env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-first-000000000", env
    assert env["RECORD_DEVICE"] == "MacBook Pro Microphone", env
    assert env["DRIVE_PARENT_FOLDER_ID"] == "folder123", "hand-added setting was lost"
    loaded = config.load_schedule(home / "schedule.toml")
    assert len(loaded.meetings) == 2, loaded
    # The full keys are shown masked, never in the clear.
    assert "sk-ant-first-000000000" not in "".join(script.prompts + script.said)
results.append(run("rerunning changes one value and keeps the rest, keys masked", t15))


def t16():
    home = Path(tempfile.mkdtemp())
    w, script = wizard(home, [
        "k1", "k2", "1",
        "Tuesday",                     # not enough fields
        "Tue 27 ACCT-4321",            # bad hour
        "Tue 14 ACCT-4321",            # good
        "",
        "y",                           # Notion yes
        "ntn_secret_000000000000",
        "https://www.notion.so/me/Tasks-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6?v=x",
    ])
    assert w.run() == 0
    out = script.output()
    assert "three things" in out, out
    assert "0-23" in out, out
    env = setup_wizard.read_env(home / ".env")
    assert env["NOTION_TOKEN"] == "ntn_secret_000000000000", env
    assert env["NOTION_DATABASE"].startswith("https://www.notion.so/"), env
    assert setup_wizard.NOTION_SKIP_MARKER not in (home / ".env").read_text()
    assert config.load_schedule(home / "schedule.toml").by_slot == {("Tue", 14): "ACCT-4321"}
results.append(run("bad schedule rows are re-asked, and Notion can be set up", t16))


def t17():
    home = Path(tempfile.mkdtemp())
    w, script = wizard(home, ["k1", "k2", "Tue 14 ACCT-4321", "", "n"], devices=[])
    assert w.run() == 0
    assert script.answers == [], script.answers
    env = setup_wizard.read_env(home / ".env")
    assert env["RECORD_DEVICE"] == config.RECORD_DEVICE, env
    assert "Microphone" in script.output()
results.append(run("with no devices to list, setup keeps going with the default mic", t17))


def t17b():
    home = Path(tempfile.mkdtemp())
    w, _ = wizard(home, ["k1", "k2", "macbook", "Tue 14 ACCT-4321", "", "n"])
    assert w.run() == 0
    env = setup_wizard.read_env(home / ".env")
    # A substring that picks out one device is stored as that device's name.
    assert env["RECORD_DEVICE"] == "MacBook Pro Microphone", env
    w, _ = wizard(home, ["", "", "Microphone", "", "n"])
    assert w.run() == 0
    # One that matches several stays a substring, which is what config expects.
    assert setup_wizard.read_env(home / ".env")["RECORD_DEVICE"] == "Microphone"
results.append(run("a unique mic substring is stored as the full device name", t17b))


def t18():
    home = Path(tempfile.mkdtemp())
    values = {"OPENAI_API_KEY": "a b#c", "ANTHROPIC_API_KEY": 'q"uote',
              "RECORD_DEVICE": "Trace’s Mic", "NOTION_TOKEN": "", "NOTION_DATABASE": ""}
    setup_wizard.write_env(home / ".env", values, notion_skipped=True)
    back = setup_wizard.read_env(home / ".env")
    for key, want in values.items():
        assert back.get(key, "") == want, (key, back.get(key))
results.append(run("values with spaces, hashes, and quotes survive the .env round trip", t18))


# --- migration -----------------------------------------------------------------

def t19():
    checkout = Path(tempfile.mkdtemp())
    (checkout / "pyproject.toml").write_text("")
    (checkout / ".env").write_text("OPENAI_API_KEY=old\n")
    (checkout / "token.json").write_text("{}")
    (checkout / "inbox").mkdir()
    (checkout / "inbox" / "lecture.m4a").write_bytes(b"x")
    (checkout / "inbox" / ".DS_Store").write_bytes(b"x")

    saved = (config.CODE_ROOT, config.HOME_DIR, config.ENV_FILE)
    home = Path(tempfile.mkdtemp())
    config.CODE_ROOT, config.HOME_DIR, config.ENV_FILE = checkout, home, home / ".env"
    try:
        found = {p.relative_to(checkout).as_posix() for p in config.legacy_files()}
        assert found == {".env", "token.json", "inbox/lecture.m4a"}, found

        script = Script(["n"])
        assert cli.offer_migration(ask=script.ask, say=script.say) is False
        assert (checkout / ".env").exists(), "declining must move nothing"

        script = Script([""])  # Enter means yes
        assert cli.offer_migration(ask=script.ask, say=script.say) is True
        assert (home / ".env").read_text() == "OPENAI_API_KEY=old\n"
        assert (home / "token.json").exists()
        assert (home / "inbox" / "lecture.m4a").exists()
        assert not (checkout / ".env").exists()
        # Once the home has a .env, nothing left in the checkout is offered.
        assert config.legacy_files() == []
    finally:
        config.CODE_ROOT, config.HOME_DIR, config.ENV_FILE = saved
results.append(run("an old install next to the code is offered, then moved, once", t19))


def t20():
    # A pipx install has no pyproject next to the package, so never offers.
    saved = config.CODE_ROOT
    config.CODE_ROOT = Path(tempfile.mkdtemp())
    (config.CODE_ROOT / ".env").write_text("x")
    try:
        assert config.legacy_files() == []
    finally:
        config.CODE_ROOT = saved
results.append(run("an installed package never mistakes site-packages for a checkout", t20))


# --- doctor ----------------------------------------------------------------------

def t21():
    saved = (config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY, config.TOKEN_FILE,
             config.SCHEDULE_FILE)
    try:
        config.OPENAI_API_KEY = ""
        config.ANTHROPIC_API_KEY = "sk-ant-test-key-0000000000"
        config.TOKEN_FILE = HOME / "no-token.json"
        config.SCHEDULE_FILE = HOME / "schedule.toml"
        checks = {c.name: c for c in (doctor.check_key("OpenAI key", "OPENAI_API_KEY"),
                                      doctor.check_key("Anthropic key", "ANTHROPIC_API_KEY"),
                                      doctor.check_schedule(), doctor.check_drive_token(),
                                      doctor.check_drive_client(), doctor.check_notion())}
        assert not checks["OpenAI key"].ok and checks["OpenAI key"].fix == "lectureai setup"
        assert checks["Anthropic key"].ok
        assert "0000000000" not in checks["Anthropic key"].detail, "key shown in full"
        assert checks["class schedule"].ok and "8 class meetings" in checks["class schedule"].detail
        assert not checks["Drive authorization"].ok
        assert checks["Drive authorization"].fix == "lectureai login"
        assert checks["Drive OAuth client"].ok, checks["Drive OAuth client"]
        assert checks["Notion"].ok and not checks["Notion"].required

        (HOME / "no-token.json").write_text(
            '{"refresh_token": "r", "scopes": ["https://www.googleapis.com/auth/drive.file"]}')
        assert doctor.check_drive_token().ok
        (HOME / "no-token.json").write_text(
            '{"token": "t", "expiry": "2020-01-01T00:00:00Z", '
            '"scopes": ["https://www.googleapis.com/auth/drive.file"]}')
        assert not doctor.check_drive_token().ok, "expired token with no refresh must fail"

        config.SCHEDULE_FILE = HOME / "broken.toml"
        config.SCHEDULE_FILE.write_text('classes = [ { day = "Nope", start = 1, course = "X" } ]')
        broken = doctor.check_schedule()
        assert not broken.ok and "row 1" in broken.detail, broken

        rendered = doctor.render(list(checks.values()))
        assert "FAIL  OpenAI key" in rendered and "fix: lectureai setup" in rendered
        assert "ok    Anthropic key" in rendered
    finally:
        (config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY, config.TOKEN_FILE,
         config.SCHEDULE_FILE) = saved
results.append(run("doctor reports each check with its fix and never a full key", t21))


def t22():
    # The whole CLI, driven without a schedule: readable, no traceback.
    saved = config.SCHEDULE_FILE
    config.SCHEDULE_FILE = HOME / "absent.toml"
    config.reload_schedule()
    import io, contextlib
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = cli.main(["record", "--minutes", "1"])
        assert code == 1, code
        assert "lectureai setup" in err.getvalue(), err.getvalue()
        assert "Traceback" not in err.getvalue()
        with contextlib.redirect_stderr(io.StringIO()):
            assert cli.main(["bogus"]) == 2
    finally:
        config.SCHEDULE_FILE = saved
        config.reload_schedule()
results.append(run("the CLI refuses to record without a schedule and says what to do", t22))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
