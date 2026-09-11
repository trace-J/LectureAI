"""Tests for record.py's device selection and naming.

Pure logic only: no microphone is opened and no ffmpeg process is started, so
this is safe to run anywhere. From the project root:

    .venv/bin/python test_record.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config  # noqa: E402
from intake import record  # noqa: E402

# Real output from `ffmpeg -f avfoundation -list_devices true -i ""`, including
# the curly apostrophe macOS puts in device names.
SAMPLE = """[AVFoundation indev @ 0x809018140] AVFoundation video devices:
[AVFoundation indev @ 0x809018140] [0] FaceTime HD Camera
[AVFoundation indev @ 0x809018140] [1] Trace’s iPhone Camera
[AVFoundation indev @ 0x809018140] [2] Capture screen 0
[AVFoundation indev @ 0x809018140] AVFoundation audio devices:
[AVFoundation indev @ 0x809018140] [0] Trace’s iPhone Microphone
[AVFoundation indev @ 0x809018140] [1] MacBook Pro Microphone
[AVFoundation indev @ 0x809018140] [2] Microsoft Teams Audio
[in#0 @ 0x809018000] Error opening input: Input/output error
"""


def fake_devices(sample=SAMPLE):
    """Parse `sample` the way list_devices parses ffmpeg's real output."""
    devices, in_audio = [], False
    for line in sample.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if "AVFoundation video devices:" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = record.DEVICE_LINE.match(line)
        if match:
            devices.append((int(match.group(1)), match.group(2).strip()))
    return devices


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    print(f"ok    {label}")
    return True


results = []

# Only audio devices, never the cameras that share the same numbering.
def t1():
    got = fake_devices()
    assert got == [(0, "Trace’s iPhone Microphone"),
                   (1, "MacBook Pro Microphone"),
                   (2, "Microsoft Teams Audio")], got
results.append(run("parses audio devices, skipping video", t1))


# The whole reason for name matching: index 0 is the iPhone, not this Mac.
def t2():
    record.list_devices = fake_devices
    index, name = record.resolve_device(None)
    assert name == "MacBook Pro Microphone", name
    assert index == 1, index
results.append(run("default picks the built-in mic, not index 0", t2))


def t3():
    record.list_devices = fake_devices
    assert record.resolve_device("teams")[0] == 2
    assert record.resolve_device("MacBook")[0] == 1
results.append(run("name substring match is case-insensitive", t3))


def t4():
    record.list_devices = fake_devices
    assert record.resolve_device("0")[1] == "Trace’s iPhone Microphone"
results.append(run("an explicit index is still honored", t4))


def t5():
    record.list_devices = fake_devices
    for bad in ("nonexistent-mic", "9"):
        try:
            record.resolve_device(bad)
        except RuntimeError:
            continue
        raise AssertionError(f"{bad!r} should have raised")
results.append(run("unknown device or index raises", t5))


# With the preferred mic unplugged, fall through rather than failing.
def t6():
    without = SAMPLE.replace("[1] MacBook Pro Microphone",
                             "[1] Some USB Headset Microphone")
    record.list_devices = lambda: fake_devices(without)
    index, name = record.resolve_device(None)
    assert "Microphone" in name, name
results.append(run("falls back when the preferred mic is absent", t6))


# Naming: the course code has to survive into the filename, because
# infer_course_from_filename is the pipeline's fallback when the schedule misses.
def t7():
    # Tuesday 14:00 is ACCT-4321 in the shipped schedule.
    when = datetime(2026, 9, 8, 14, 0)
    name = record.output_name(when)
    assert name == "ACCT-4321_2026-09-08_1400.m4a", name
    assert config.infer_course_from_filename(name) == "ACCT-4321"
results.append(run("scheduled class is named for its course", t7))


def t8():
    when = datetime(2026, 9, 6, 3, 0)  # a Sunday, no class
    name = record.output_name(when)
    assert name == "lecture_2026-09-06_0300.m4a", name
results.append(run("unscheduled recording gets a neutral name", t8))


def t9():
    when = datetime(2026, 9, 6, 3, 0)
    name = record.output_name(when, course="RELI-3304")
    assert name == "RELI-3304_2026-09-06_0300.m4a", name
    assert config.infer_course_from_filename(name) == "RELI-3304"
results.append(run("an explicit course overrides the schedule", t9))


# The date in the name must not be mistaken for a course code.
def t10():
    name = record.output_name(datetime(2026, 9, 8, 14, 0))
    assert config.infer_course_from_filename(name) == "ACCT-4321"
    neutral = record.output_name(datetime(2026, 9, 6, 3, 0))
    assert config.infer_course_from_filename(neutral) == config.UNKNOWN_COURSE
results.append(run("the date in a name invents no course", t10))


# --- a recording nobody is holding ----------------------------------------
#
# ffmpeg runs in its own session and outlives the panel or terminal that
# started it. The state file is how the next process finds it again.

import os  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from intake import transcribe  # noqa: E402

STATE = config.RECORDING_STATE_FILE
STAGING = config.WORK_DIR / "recording_20260910-123340.m4a"
STARTED = datetime(2026, 9, 10, 12, 33, 40)  # a Thursday: ENTR-3306 at 12

# Adopting or starting a recording asks caffeinate to hold the Mac awake for
# ffmpeg's lifetime. The tests stand a `sleep` in for ffmpeg, and t22 checks
# the real function on its own.
REAL_KEEP_AWAKE = record._keep_awake
record._keep_awake = lambda pid: None


def reset():
    STATE.unlink(missing_ok=True)
    STAGING.unlink(missing_ok=True)
    STAGING.with_suffix(".log").unlink(missing_ok=True)
    config.LOG_FILE.unlink(missing_ok=True)
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    for p in config.INBOX_DIR.iterdir():
        p.unlink()


def log_lines():
    if not config.LOG_FILE.exists():
        return []
    return [line.split("\t") for line in config.LOG_FILE.read_text().splitlines()]


def t11():
    cmd = record._ffmpeg_command(1, STAGING, 240)
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "14400", cmd
    assert cmd[-1] == str(STAGING) and cmd[-2] == "-y", cmd
    assert "-t" not in record._ffmpeg_command(1, STAGING, None)
results.append(run("ffmpeg itself is told when to stop", t11))


def t12():
    reset()
    assert record.Recorder.adopt() is None
    assert record.finish_abandoned() is None
    STATE.write_text("not json")
    assert record.Recorder.adopt() is None
    assert record.finish_abandoned() is None
    reset()
results.append(run("no state file, or a broken one, means nothing to adopt", t12))


def t13():
    reset()
    # A pid that is not running: the ffmpeg was killed outright, so the file
    # has no trailer. It stays where it is, but the state file goes.
    STAGING.write_bytes(b"x" * 1000)
    record._write_state(2 ** 22 - 1, STAGING, STARTED, None, "MacBook Pro Microphone")
    real = transcribe.duration_seconds
    transcribe.duration_seconds = lambda p: None
    try:
        assert record.Recorder.adopt() is None
        assert record.finish_abandoned() is None
    finally:
        transcribe.duration_seconds = real
    assert not STATE.exists(), "stale state must be cleared"
    assert STAGING.exists(), "an unplayable file must not be deleted"
    assert list(config.INBOX_DIR.iterdir()) == []
    reset()
results.append(run("a dead ffmpeg with an unplayable file is cleared, not filed", t13))


def t14():
    reset()
    # ffmpeg reached its time cap, finalized the file, and exited; the panel
    # that started it was already gone. The lecture must still reach inbox/.
    STAGING.write_bytes(b"x" * 1000)
    record._write_state(2 ** 22 - 1, STAGING, STARTED, None, "MacBook Pro Microphone")
    real = transcribe.duration_seconds
    transcribe.duration_seconds = lambda p: 4573.0
    try:
        filed = record.finish_abandoned()
    finally:
        transcribe.duration_seconds = real
    assert filed is not None and filed.parent == config.INBOX_DIR, filed
    assert filed.name == "ENTR-3306_2026-09-10_1233.m4a", filed.name
    assert not STATE.exists() and not STAGING.exists()
    assert record.finish_abandoned() is None
    reset()
results.append(run("a finished recording nobody moved is filed into the inbox", t14))


def t15():
    reset()
    # A live process standing in for ffmpeg. Its command line is not ffmpeg's,
    # so the ownership check is stubbed; t16 covers the check itself.
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    STAGING.write_bytes(b"x" * 1000)
    record._write_state(proc.pid, STAGING, STARTED, "RELI-3304", "Some Mic")
    real = record._process_is_recording
    record._process_is_recording = lambda pid, staging: pid == proc.pid
    try:
        rec = record.Recorder.adopt()
        assert rec is not None and rec.adopted, "a live pid must be adopted"
        assert rec.is_recording
        assert rec.started == STARTED and rec.course == "RELI-3304", (rec.started, rec.course)
        assert rec.device_name == "Some Mic"
        assert rec.staged_bytes == 1000
        assert rec.planned_name == "RELI-3304_2026-09-10_1233.m4a", rec.planned_name
        assert rec.elapsed > 60, rec.elapsed
        # finish_abandoned must leave a live recording alone.
        assert record.finish_abandoned() is None and STATE.exists()
        # A second start must refuse rather than open the mic twice.
        try:
            record.Recorder().start()
        except RuntimeError as exc:
            assert "already running" in str(exc), exc
        else:
            raise AssertionError("starting over a live recording should raise")
        filed = rec.stop()
    finally:
        record._process_is_recording = real
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    assert proc.returncode is not None, "stop must end the process"
    assert filed.name == "RELI-3304_2026-09-10_1233.m4a", filed
    assert filed.exists() and not STAGING.exists()
    assert not STATE.exists(), "stopping must clear the state file"
    assert not rec.is_recording
    reset()
results.append(run("a live recording from an earlier process is adopted and stopped", t15))


def t16():
    reset()
    # Ownership is pid + command line, so a reused pid can't be mistaken for
    # our ffmpeg and killed.
    assert not record._process_is_recording(2 ** 22 - 1, STAGING)
    assert not record._process_is_recording(os.getpid(), STAGING), \
        "this test process is not ffmpeg"
    reset()
results.append(run("a live pid that is not our ffmpeg is not adopted", t16))


def t17():
    reset()
    # start() records what it launched, without launching anything here.
    launched = []

    class FakePopen:
        pid = 4242
        def __init__(self, cmd, **kw):
            launched.append((cmd, kw))
        def poll(self):
            return None

    real_popen, real_which, real_awake = (record.subprocess.Popen, record.shutil.which,
                                          record._keep_awake)
    record.subprocess.Popen = FakePopen
    record.shutil.which = lambda name: f"/opt/homebrew/bin/{name}"
    record._keep_awake = REAL_KEEP_AWAKE
    record.list_devices = fake_devices
    try:
        rec = record.Recorder(course="RELI-3304")
        rec.start(max_minutes=90)
    finally:
        record.subprocess.Popen, record.shutil.which = real_popen, real_which
        record._keep_awake = real_awake
    cmds = {cmd[0]: (cmd, kw) for cmd, kw in launched}
    assert set(cmds) == {"ffmpeg", "caffeinate"}, [c for c, _ in launched]
    cmd, kw = cmds["ffmpeg"]
    assert kw["start_new_session"] is True
    assert cmd[cmd.index("-t") + 1] == "5400", cmd
    # Idle sleep ends the capture, so the Mac is held awake for exactly as
    # long as ffmpeg runs, by pid, with nothing to clean up afterwards.
    awake, _ = cmds["caffeinate"]
    assert awake[awake.index("-w") + 1] == "4242" and "-i" in awake, awake
    state = record._read_state()
    assert state is not None, "start must write the state file"
    assert state["pid"] == 4242 and state["course"] == "RELI-3304", state
    assert state["staging"] == rec._staging and state["started"] == rec.started, state
    assert state["device"] == "MacBook Pro Microphone", state
    assert rec._errors == rec._staging.with_suffix(".log") and rec._errors.exists()
    rec._errors.unlink()
    reset()
results.append(run("start writes the state file the next process will read", t17))


def t18():
    reset()
    # Two front ends held the same recording and the other one stopped it:
    # the file is gone and so is the state. That is not a failed recording.
    gone = record.Recorder(course="RELI-3304")
    gone.started = STARTED
    gone._staging = STAGING
    gone._proc = record._ExternalProcess(2 ** 22 - 1)
    assert gone.finish() is None
    assert not gone.is_recording
    # Whereas a recording that ended on its own with its file in place is filed.
    STAGING.write_bytes(b"x" * 1000)
    ended = record.Recorder(course="RELI-3304")
    ended.started = STARTED
    ended._staging = STAGING
    ended._proc = record._ExternalProcess(2 ** 22 - 1)
    filed = ended.finish()
    assert filed is not None and filed.name == "RELI-3304_2026-09-10_1233.m4a", filed
    assert filed.exists() and not STAGING.exists()
    reset()
results.append(run("finish() tells a recording filed elsewhere from one that ended", t18))

# --- a recording that captured nothing ------------------------------------
#
# A lecture once ran 50 minutes with the timer ticking while the microphone
# delivered nothing; the failure flashed for six seconds, went to a terminal
# nobody was watching, and the cleanup deleted the only evidence.

def t19():
    reset()
    errors = STAGING.with_suffix(".log")
    errors.write_text("[avfoundation] Input/output error\n")
    rec = record.Recorder(course="RELI-3304")
    rec.started = STARTED
    rec.device_name = "MacBook Pro Microphone"
    rec._staging, rec._errors = STAGING, errors
    rec._proc = record._ExternalProcess(2 ** 22 - 1)  # ffmpeg already gone
    try:
        rec.stop()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("a recording with no file must raise")
    assert "Input/output error" in message, message
    assert str(errors) in message, "the kept log must be named"
    assert errors.exists(), "ffmpeg's log is the only evidence; it must be kept"
    assert not STATE.exists()
    # pipeline.log carries the failure, named for the lecture that was lost,
    # on one line, so the panel's recent list shows it.
    lines = log_lines()
    assert len(lines) == 1 and lines[0][1] == "ERROR", lines
    assert lines[0][2] == "RELI-3304_2026-09-10_1233.m4a", lines[0]
    assert "no audio" in lines[0][3] and "\n" not in lines[0][3], lines[0]
    assert list(config.INBOX_DIR.iterdir()) == []
    reset()
results.append(run("a recording that produced nothing is logged and its ffmpeg log kept", t19))


def t20():
    reset()
    # The same failure with an empty file and a silent ffmpeg: the empty file
    # and empty log go, the failure still lands in pipeline.log.
    STAGING.write_bytes(b"")
    errors = STAGING.with_suffix(".log")
    errors.write_text("")
    rec = record.Recorder(course="RELI-3304")
    rec.started = STARTED
    rec.device_name = "MacBook Pro Microphone"
    rec._staging, rec._errors = STAGING, errors
    rec._proc = record._ExternalProcess(2 ** 22 - 1)
    try:
        rec.stop()
    except RuntimeError as exc:
        assert "Microphone" in str(exc) and "no error of its own" in str(exc), exc
    else:
        raise AssertionError("an empty file must raise")
    assert not STAGING.exists() and not errors.exists()
    assert len(log_lines()) == 1 and log_lines()[0][1] == "ERROR", log_lines()

    # Found later by another process instead: same outcome.
    reset()
    STAGING.write_bytes(b"")
    record._write_state(2 ** 22 - 1, STAGING, STARTED, "RELI-3304", "Some Mic")
    assert record.finish_abandoned() is None
    assert not STATE.exists() and not STAGING.exists()
    lines = log_lines()
    assert len(lines) == 1 and lines[0][2] == "RELI-3304_2026-09-10_1233.m4a", lines
    assert "Some Mic" in lines[0][3], lines[0]
    reset()
results.append(run("an empty recording, stopped or found abandoned, reaches pipeline.log", t20))


def t21():
    reset()

    class Alive:
        pid = 4242
        def poll(self):
            return None

    from datetime import timedelta
    rec = record.Recorder(course="RELI-3304")
    rec.device_name = "MacBook Pro Microphone"
    rec._staging = STAGING
    rec._proc = Alive()
    STAGING.write_bytes(b"x" * 44)  # the m4a header ffmpeg writes at once

    rec.started = datetime.now() - timedelta(seconds=5)
    assert not rec.stalled, "too early to tell"
    assert rec.warning == ""

    rec.started = datetime.now() - timedelta(seconds=config.RECORD_NO_AUDIO_SECONDS + 1)
    assert rec.stalled, "a header-only file after the grace period is a stall"
    assert "MacBook Pro Microphone" in rec.warning and "Microphone" in rec.warning, rec.warning

    STAGING.write_bytes(b"x" * (config.RECORD_NO_AUDIO_BYTES + 1))
    assert not rec.stalled, "audio on disk means the mic is live"

    rec._proc = record._ExternalProcess(2 ** 22 - 1)  # gone
    STAGING.write_bytes(b"x" * 44)
    assert not rec.stalled, "a finished recording is not stalled, it is over"
    reset()
results.append(run("a mic that delivers nothing is called out after the grace period", t21))


def t22():
    launched = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            launched.append((cmd, kw))

    real_popen, real_which = record.subprocess.Popen, record.shutil.which
    record.subprocess.Popen = FakePopen
    try:
        record.shutil.which = lambda name: None
        REAL_KEEP_AWAKE(1234)
        assert launched == [], "no caffeinate on this system means nothing to run"
        record.shutil.which = lambda name: "/usr/bin/caffeinate"
        REAL_KEEP_AWAKE(1234)
    finally:
        record.subprocess.Popen, record.shutil.which = real_popen, real_which
    assert len(launched) == 1, launched
    cmd, kw = launched[0]
    assert cmd[0] == "caffeinate" and cmd[-2:] == ["-w", "1234"], cmd
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL
results.append(run("caffeinate follows ffmpeg's pid and is skipped where it does not exist", t22))


def t23():
    # Whether a watcher is running is read from the lock, not from kill(0):
    # a pid in the file with nobody holding the lock is a dead watcher.
    import fcntl
    lock = config.LOCK_FILE
    lock.write_text(str(os.getpid()))
    assert not record._watcher_is_running(), "an unheld lock is a stopped watcher"
    handle = lock.open("r+")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert record._watcher_is_running(), "a held lock is a running watcher"
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
    assert not record._watcher_is_running()
    lock.unlink()
    assert not record._watcher_is_running()
results.append(run("the watcher is running when its lock is held, not when its pid exists", t23))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
