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


def require(name: str) -> str:
    """Fetch a required setting or fail with a readable message."""
    value = globals().get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Add it to {BASE_DIR / '.env'} "
            f"(see .env.example) and try again."
        )
    return value
