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

fresh_home()  # before config is imported, so nothing touches ~/.lectureai

from lectureai import config  # noqa: E402
from lectureai import record  # noqa: E402

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

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
