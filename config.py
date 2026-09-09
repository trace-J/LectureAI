"""Settings, class schedule, and paths for the lecture pipeline."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

# --- API keys -------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Paths ----------------------------------------------------------------

INBOX_DIR = BASE_DIR / "inbox"
PROCESSED_DIR = BASE_DIR / "processed"
WORK_DIR = BASE_DIR / ".work"          # scratch space for compressed/split audio
LOG_FILE = BASE_DIR / "pipeline.log"
# What the watcher is doing right now, for the control panel to read. Written
# during a run and removed at the end; pipeline.log only gets a line once a
# lecture is finished, which leaves the whole transcription invisible.
STATUS_FILE = BASE_DIR / ".work" / "status.json"
LOCK_FILE = BASE_DIR / ".watcher.lock"   # guards against two watchers at once

CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

for _d in (INBOX_DIR, PROCESSED_DIR, WORK_DIR):
    _d.mkdir(exist_ok=True)

# --- Google Drive ---------------------------------------------------------

# drive.file is the only Drive scope Google treats as non-sensitive: it needs
# no app verification, and its refresh tokens don't expire after 7 days the way
# a Testing-mode app's do. The tradeoff is that it only reaches files this app
# created — so the app creates its own root folder rather than writing into one
# you made by hand. Move that folder anywhere in Drive afterward; per-file
# access follows it. Delete token.json and re-authorize if you change this.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# The app creates this folder in My Drive on first upload and files courses
# under it. Its id is cached in DRIVE_ROOT_CACHE so renaming or moving the
# folder in Drive doesn't matter.
DRIVE_ROOT_FOLDER_NAME = os.getenv("DRIVE_ROOT_FOLDER_NAME", "Lecture Notes")
DRIVE_ROOT_CACHE = BASE_DIR / ".drive_root"

# Optional override: pin a specific folder id instead. Only works for a folder
# this app created, given the drive.file scope above.
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID", "")

# --- Models ---------------------------------------------------------------

# Swap to "whisper-1" here to fall back to Whisper; nothing else changes.
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
CLAUDE_MODEL = "claude-sonnet-5"

# --- Audio handling -------------------------------------------------------

AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav"}

WHISPER_LIMIT_BYTES = 25 * 1024 * 1024   # hard API file-size limit
COMPRESS_THRESHOLD_BYTES = 24 * 1024 * 1024  # compress before we get close to it

# The gpt-4o transcribe models cap their OUTPUT near 2000 tokens (~1740 words)
# and truncate silently rather than erroring, so long audio must be split on
# duration as well as size. Measured against a real lecture: clean through
# 12 min at 145 words/min, truncated at 15 min and beyond.
#
# 8 minutes leaves headroom for fast talkers (safe to roughly 210 words/min).
# Lower it if you see truncation warnings; raise it toward 12 for slow ones.
# whisper-1 has no such output cap, so 20 * 60 is fine when using that model.
CHUNK_SECONDS = 8 * 60

# A chunk coming back at or above this word count probably got cut off.
TRUNCATION_WORD_THRESHOLD = 1700

# --- Output handling ---

# Upload the summary as a real Google Doc rather than a .md file.
SUMMARY_AS_GOOGLE_DOC = True

# Transcripts go in this subfolder of the course folder, keeping the course
# folder itself a clean list of study notes. Set to "" to file them alongside.
TRANSCRIPT_SUBFOLDER = "Transcripts"

# Delete the recording once both uploads succeed, instead of archiving it in
# processed/. Set to False to keep the originals.
DELETE_ORIGINAL_AFTER_UPLOAD = True

# --- Notion (notion_tasks.py) ---------------------------------------------

# Internal integration secret from notion.so/my-integrations. Leave it unset
# and the pipeline simply skips Notion; nothing else changes.
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")

# The to-do database action items are added to. Paste the whole Notion URL;
# the id is pulled out of it.
NOTION_DATABASE = os.getenv("NOTION_DATABASE", "")

# Pinned deliberately. 2025-09-03 split databases from data sources, changing
# the page parent and the query endpoint, so the version and the request
# shapes in notion_tasks.py have to move together.
NOTION_VERSION = os.getenv("NOTION_VERSION", "2026-03-11")

# Where action items go: "weekly" writes a checkbox into the day column of
# the weekly page, which is the list you actually tick; "database" creates a
# row with Due/Course/Source fields instead. They are different surfaces, and
# a row is invisible from the weekly page.
NOTION_TARGET = os.getenv("NOTION_TARGET", "weekly")

# Optional overrides if the automatic property matching picks wrong. Each is
# the exact property name in your database.
NOTION_PROP_DUE = os.getenv("NOTION_PROP_DUE", "")
NOTION_PROP_COURSE = os.getenv("NOTION_PROP_COURSE", "")
NOTION_PROP_KIND = os.getenv("NOTION_PROP_KIND", "")
NOTION_PROP_SOURCE = os.getenv("NOTION_PROP_SOURCE", "")

# --- Recording (record.py) ------------------------------------------------

# Which microphone to record from. Either a substring of the device name as
# ffmpeg reports it ("MacBook Pro") or an avfoundation index ("1").
#
# Prefer a name. Indices are assigned in connection order, so plugging in a
# headset or waking a nearby iPhone renumbers them: on this Mac index 0 is
# often the iPhone's mic rather than the built-in one, and a lecture recorded
# through a phone that then leaves the room is a lecture you don't have.
RECORD_DEVICE = os.getenv("RECORD_DEVICE", "MacBook Pro Microphone")

# Names to fall back through if RECORD_DEVICE matches nothing attached.
RECORD_DEVICE_FALLBACKS = ("MacBook Pro Microphone", "Built-in", "Microphone")

# Mono at 16 kHz is what the transcription models resample to anyway, so
# anything richer is bytes spent on quality that gets discarded.
RECORD_SAMPLE_RATE = 16000
RECORD_CHANNELS = 1
RECORD_BITRATE = "64k"

# Stop on your own; this only guards against a recorder left running all night.
RECORD_MAX_MINUTES = 240

# --- Watcher ---

# Phone sync writes incrementally, so a file isn't ready the moment it appears.
STABILITY_SECONDS = 10        # size must hold steady this long
STABILITY_POLL_SECONDS = 2    # how often to re-check the size
STABILITY_TIMEOUT_SECONDS = 3600  # give up waiting on a file still growing

# --- Class schedule -------------------------------------------------------

# (day abbreviation, start hour in 24h local time) -> course code
SCHEDULE: dict[tuple[str, int], str] = {
    ("Mon", 9): "ENTR-4306",
    ("Tue", 12): "ENTR-3306",
    ("Tue", 14): "ACCT-4321",
    ("Wed", 9): "ENTR-4306",
    ("Thu", 12): "ENTR-3306",
    ("Thu", 14): "ACCT-4321",
    ("Fri", 9): "ENTR-4306",
    ("Fri", 12): "RELI-3304",
}

# How far a recording's START may sit from a class start and still match.
# Measured from the start, not the file mtime, so this must stay well under
# the gap between back-to-back classes: with ENTR-3306 at 12:00 and ACCT-4321
# at 14:00 on Tue/Thu, anything near 60 makes the two windows meet.
SCHEDULE_TOLERANCE_MINUTES = 45

UNKNOWN_COURSE = "UNKNOWN"


def recording_start(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> datetime:
    """When the recording began.

    A file's mtime is when recording *stopped*, so for an 80 minute class it
    lands 80 minutes after the class began — closer to the next class on the
    calendar than to its own. Backing out the audio duration removes that bias
    and is what makes back-to-back classes distinguishable.
    """
    when = (
        file_mtime
        if isinstance(file_mtime, datetime)
        else datetime.fromtimestamp(file_mtime)
    )
    if duration_seconds:
        when -= timedelta(seconds=duration_seconds)
    return when


def infer_course(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """Map a recording to a course code from SCHEDULE.

    Matches the class whose start is nearest the recording's start, within
    SCHEDULE_TOLERANCE_MINUTES. Pass duration_seconds whenever you have it;
    without it this falls back to comparing against the mtime, which skews
    toward the following class.
    """
    when = recording_start(file_mtime, duration_seconds)
    day = when.strftime("%a")  # "Mon", "Tue", ...

    best_course = UNKNOWN_COURSE
    best_delta = None

    for (sched_day, sched_hour), course in SCHEDULE.items():
        if sched_day != day:
            continue
        start = when.replace(hour=sched_hour, minute=0, second=0, microsecond=0)
        delta_minutes = abs((when - start).total_seconds()) / 60
        if delta_minutes > SCHEDULE_TOLERANCE_MINUTES:
            continue
        if best_delta is None or delta_minutes < best_delta:
            best_delta = delta_minutes
            best_course = course

    return best_course


# Matches a course code in a filename: "acct-4321", "ACCT4321", "acct_4321".
COURSE_CODE_RE = re.compile(r"([A-Za-z]{2,4})[-_ ]?(\d{4})")


def infer_course_from_filename(filename: str) -> str:
    """Pull a known course code out of a filename, or UNKNOWN_COURSE.

    Only codes that appear in SCHEDULE are accepted, so a date or a random
    number in the name can't invent a course folder.
    """
    known = {code.upper(): code for code in SCHEDULE.values()}
    for match in COURSE_CODE_RE.finditer(filename):
        candidate = f"{match.group(1).upper()}-{match.group(2)}"
        if candidate in known:
            return known[candidate]
    return UNKNOWN_COURSE


def resolve_course(
    path: str | Path,
    file_mtime: float | datetime,
    duration_seconds: float | None = None,
) -> tuple[str, str]:
    """Best guess at the course, plus which signal produced it.

    The schedule wins when it matches. The filename is the fallback, which
    saves recordings whose mtime is the time they were copied rather than the
    time they were recorded — a plain `cp` does exactly that.
    """
    scheduled = infer_course(file_mtime, duration_seconds)
    if scheduled != UNKNOWN_COURSE:
        return scheduled, "schedule"

    from_name = infer_course_from_filename(Path(path).name)
    if from_name != UNKNOWN_COURSE:
        return from_name, "filename"

    return UNKNOWN_COURSE, "none"


def lecture_date(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """YYYY-MM-DD of the class, used in output filenames."""
    return recording_start(file_mtime, duration_seconds).strftime("%Y-%m-%d")


def next_class_meeting(
    course: str, after: datetime | str, within_days: int = 21
) -> str | None:
    """The next date `course` meets after `after`, as YYYY-MM-DD.

    Used to date an action item the instructor never put a deadline on.
    Readings and problem sets are usually due the next time the class meets,
    which beats leaving the task undated and letting it sink in a to-do list
    sorted by date. Returns None for a course that isn't in SCHEDULE, or one
    that doesn't meet again inside `within_days`.
    """
    if isinstance(after, str):
        try:
            after = datetime.strptime(after[:10], "%Y-%m-%d")
        except ValueError:
            return None

    meeting_days = {day for (day, _hour), code in SCHEDULE.items() if code == course}
    if not meeting_days:
        return None

    for offset in range(1, within_days + 1):
        candidate = after + timedelta(days=offset)
        if candidate.strftime("%a") in meeting_days:
            return candidate.strftime("%Y-%m-%d")
    return None


def recording_key(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """Stable identity for one recording: the minute it started.

    Stamped onto the uploaded Drive files so a re-run can find and replace what
    it produced last time, even if the topic slug came back different and the
    filename changed with it. Two recordings can't start in the same minute, so
    matching keys mean the same lecture and differing keys mean different ones.

    Derived from the file's mtime and duration, both fixed once recording
    stops, so the same file always produces the same key.
    """
    return recording_start(file_mtime, duration_seconds).strftime("%Y-%m-%dT%H:%M")


def recording_time_suffix(
    file_mtime: float | datetime, duration_seconds: float | None = None
) -> str:
    """HHMM of the recording's start, to tell same-day recordings apart."""
    return recording_start(file_mtime, duration_seconds).strftime("%H%M")


def require(name: str) -> str:
    """Fetch a required setting or fail with a readable message."""
    value = globals().get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {BASE_DIR / '.env'} "
            f"(see .env.example) and try again."
        )
    return value
