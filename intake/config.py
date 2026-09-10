"""Settings, class schedule, and paths for the lecture pipeline.

Everything the pipeline reads or writes lives in one home directory, separate
from the code: $INTAKE_HOME if set, otherwise ~/.intake ($LECTUREAI_HOME, the
variable's name before the rename, is still honored when the new one is
unset). The code directory holds only code, so the same install serves any Mac
and the repo never fills up with recordings, tokens, and logs.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# --- Where things live -----------------------------------------------------

PACKAGE_DIR = Path(__file__).resolve().parent

# The checkout this package was loaded from, when it is one. Under pipx this
# is a site-packages directory and nothing below looks for anything in it.
CODE_ROOT = PACKAGE_DIR.parent

HOME_ENV_VAR = "INTAKE_HOME"
# The variable's name before the rename to intake. Still read when the new one
# is unset, so an existing install keeps finding its data without anyone
# editing a shell profile. Set INTAKE_HOME instead; this one goes away later.
LEGACY_HOME_ENV_VAR = "LECTUREAI_HOME"
DEFAULT_HOME = Path("~/.intake")
# Where the data lived before the rename. A default install with nothing in
# ~/.intake yet is offered a move from here (see legacy_files below).
OLD_DEFAULT_HOME = Path("~/.lectureai")


def home_source(env: dict | None = None) -> str:
    """Which setting decides the home: the env var's name, or "default"."""
    source = os.environ if env is None else env
    for name in (HOME_ENV_VAR, LEGACY_HOME_ENV_VAR):
        if (source.get(name) or "").strip():
            return name
    return "default"


def resolve_home(env: dict | None = None) -> Path:
    """The data directory: $INTAKE_HOME if set, else $LECTUREAI_HOME, else ~/.intake.

    Takes the environment as an argument so tests can resolve against a fake
    one without touching the process environment.
    """
    source = os.environ if env is None else env
    name = home_source(source)
    chosen = DEFAULT_HOME if name == "default" else Path(source[name].strip())
    return chosen.expanduser().resolve()


HOME_DIR = resolve_home()
HOME_SOURCE = home_source()

# Kept for anything that still spells the old name. New code should say
# HOME_DIR, which is what this has always meant: where the data goes.
BASE_DIR = HOME_DIR

ENV_FILE = HOME_DIR / ".env"
load_dotenv(ENV_FILE)

# --- API keys -------------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Paths ----------------------------------------------------------------

INBOX_DIR = HOME_DIR / "inbox"
PROCESSED_DIR = HOME_DIR / "processed"
WORK_DIR = HOME_DIR / ".work"          # scratch space for compressed/split audio
LOG_FILE = HOME_DIR / "pipeline.log"
# What the watcher is doing right now, for the control panel to read. Written
# during a run and removed at the end; pipeline.log only gets a line once a
# lecture is finished, which leaves the whole transcription invisible.
STATUS_FILE = WORK_DIR / "status.json"
LOCK_FILE = HOME_DIR / ".watcher.lock"   # guards against two watchers at once
# The recording in progress, if any: ffmpeg's pid, where it is writing, when it
# began. ffmpeg is started in its own session so it survives whoever started
# it; this file is how the next panel or CLI finds it again and stops it
# properly instead of leaving a lecture recording with nobody at the controls.
RECORDING_STATE_FILE = WORK_DIR / "recording.json"

# A Google OAuth client placed here overrides the one bundled with the
# package (see google_client.py). Almost nobody needs to.
CREDENTIALS_FILE = HOME_DIR / "credentials.json"
TOKEN_FILE = HOME_DIR / "token.json"

SCHEDULE_FILE = HOME_DIR / "schedule.toml"


def ensure_home() -> Path:
    """Create the home directory and its subfolders. Safe to call repeatedly."""
    for directory in (HOME_DIR, INBOX_DIR, PROCESSED_DIR, WORK_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    return HOME_DIR


ensure_home()

# --- Migration from an older install ---------------------------------------
#
# Two kinds of older install leave data where this version does not look: one
# from before the home directory existed kept everything next to the code, and
# one from before the rename kept it in ~/.lectureai. Either is offered a move
# into the home directory the first time the CLI runs against an empty one,
# and the same code does the moving.

# Data files an older install kept next to the code.
LEGACY_FILES = (".env", "token.json", ".drive_root", "pipeline.log")
# The old home held these as well; setup wrote them there, never to a checkout.
LEGACY_HOME_FILES = LEGACY_FILES + ("schedule.toml", "credentials.json")
LEGACY_DIRS = ("inbox", "processed")


def old_default_home() -> Path | None:
    """~/.lectureai, when this install uses the new default and it is on disk.

    Only the default home is a candidate. Someone who set $INTAKE_HOME or
    $LECTUREAI_HOME has said where their data is, and the tests, which always
    set one, must never be offered the real thing.
    """
    if HOME_SOURCE != "default":
        return None
    old = OLD_DEFAULT_HOME.expanduser().resolve()
    if old == HOME_DIR or not old.is_dir():
        return None
    return old


def legacy_roots() -> list[tuple[Path, tuple[str, ...]]]:
    """Where an older install may have left data, and the file names to look for."""
    roots: list[tuple[Path, tuple[str, ...]]] = []
    old_home = old_default_home()
    if old_home is not None:
        roots.append((old_home, LEGACY_HOME_FILES))
    if (CODE_ROOT / "pyproject.toml").exists() and CODE_ROOT != HOME_DIR:
        roots.append((CODE_ROOT, LEGACY_FILES))
    return roots


def data_files_in(root: Path, names: tuple[str, ...]) -> list[Path]:
    """The named files plus the recordings in inbox/ and processed/ under root."""
    found = [root / name for name in names if (root / name).is_file()]
    for sub in LEGACY_DIRS:
        folder = root / sub
        if folder.is_dir():
            found += sorted(p for p in folder.iterdir()
                            if p.is_file() and not p.name.startswith("."))
    return found


def legacy_root(path: Path) -> Path:
    """The older-install directory a path from legacy_files() came from."""
    for root, _names in legacy_roots():
        if root in path.parents:
            return root
    raise ValueError(f"{path} is not inside an older install")


def legacy_files() -> list[Path]:
    """Data files an older install left behind: old home first, then checkout.

    Only meaningful when the home directory has no .env yet: once it does, the
    move has happened (or was declined) and anything left elsewhere is the
    owner's business.
    """
    if ENV_FILE.exists():
        return []
    found: list[Path] = []
    for root, names in legacy_roots():
        found += data_files_in(root, names)
    return found


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
DRIVE_ROOT_CACHE = HOME_DIR / ".drive_root"

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
#
# The schedule is a file in the home directory, schedule.toml, written by
# `intake setup` and editable by hand: one row per class meeting with the
# day, the start hour, and the course code. It is read on first use and
# cached, so a missing or broken file is reported by whichever command needs
# it rather than by every import.

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_DAY_ALIASES = {
    "mon": "Mon", "monday": "Mon",
    "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
    "wed": "Wed", "weds": "Wed", "wednesday": "Wed",
    "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu",
    "fri": "Fri", "friday": "Fri",
    "sat": "Sat", "saturday": "Sat",
    "sun": "Sun", "sunday": "Sun",
}

# How far a recording's START may sit from a class start and still match.
# Measured from the start, not the file mtime, so this must stay well under
# the gap between back-to-back classes: with one class at 12:00 and the next
# at 14:00 on the same day, anything near 60 makes the two windows meet.
DEFAULT_TOLERANCE_MINUTES = 45

UNKNOWN_COURSE = "UNKNOWN"

# Matches a course code in a filename: "acct-4321", "ACCT4321", "acct_4321".
COURSE_CODE_RE = re.compile(r"([A-Za-z]{2,4})[-_ ]?(\d{4})")


class ScheduleError(RuntimeError):
    """The schedule file is missing or cannot be read."""


@dataclass(frozen=True)
class Meeting:
    day: str      # "Mon" .. "Sun"
    hour: int     # start hour, 24h local time
    course: str


@dataclass(frozen=True)
class Schedule:
    meetings: tuple[Meeting, ...]
    tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES

    @property
    def by_slot(self) -> dict[tuple[str, int], str]:
        """(day, start hour) -> course code, the shape the matching uses."""
        return {(m.day, m.hour): m.course for m in self.meetings}

    def courses(self) -> list[str]:
        return sorted({m.course for m in self.meetings})


def normalize_day(raw: object) -> str:
    """"tuesday", "Tues", "TUE" -> "Tue". Raises ValueError for anything else."""
    key = str(raw).strip().lower()
    if key not in _DAY_ALIASES:
        raise ValueError(f"day must be one of {', '.join(DAYS)}, not {raw!r}")
    return _DAY_ALIASES[key]


def normalize_hour(raw: object) -> int:
    """A start hour as an int 0..23. Accepts 14, "14", "14:00", "2pm"."""
    if isinstance(raw, bool):
        raise ValueError(f"start hour must be a number 0-23, not {raw!r}")
    if isinstance(raw, int):
        hour = raw
    else:
        text = str(raw).strip().lower().replace(" ", "")
        match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", text)
        if not match:
            raise ValueError(f"start hour must be a number 0-23, not {raw!r}")
        hour = int(match.group(1))
        if match.group(2) and match.group(2) != "00":
            raise ValueError(
                f"start hour must be a whole hour ({raw!r} has minutes); the "
                f"tolerance window covers classes that start on the half hour"
            )
        suffix = match.group(3)
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    if not 0 <= hour <= 23:
        raise ValueError(f"start hour must be 0-23, not {raw!r}")
    return hour


def normalize_course(raw: object) -> str:
    """"acct-4321", "ACCT4321", "acct_4321" -> "ACCT-4321".

    Anything shaped like a course code is written the one way the filename
    fallback and the Drive folders expect. A code with another shape is kept
    exactly as typed.
    """
    text = str(raw).strip()
    if not text:
        raise ValueError("course code is empty")
    match = COURSE_CODE_RE.fullmatch(text)
    if match:
        return f"{match.group(1).upper()}-{match.group(2)}"
    return text


def parse_schedule(text: str, source: str = "schedule.toml") -> Schedule:
    """Parse the TOML text of a schedule file into a Schedule.

    Raises ScheduleError naming the row that is wrong, so a typo in one line
    points at that line rather than at the whole file.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ScheduleError(f"{source} is not valid TOML: {exc}") from None

    tolerance = data.get("tolerance_minutes", DEFAULT_TOLERANCE_MINUTES)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
        raise ScheduleError(
            f"{source}: tolerance_minutes must be a whole number of minutes, "
            f"not {tolerance!r}"
        )

    rows = data.get("classes")
    if rows is None:
        raise ScheduleError(
            f"{source} has no `classes` list. Each class meeting is one row "
            f"like  {{ day = \"Tue\", start = 14, course = \"ACCT-4321\" }}"
        )
    if not isinstance(rows, list):
        raise ScheduleError(f"{source}: `classes` must be a list of rows")

    meetings: list[Meeting] = []
    for n, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ScheduleError(
                f"{source}: class row {n} should look like "
                f"{{ day = \"Tue\", start = 14, course = \"ACCT-4321\" }}, "
                f"not {row!r}"
            )
        missing = [k for k in ("day", "start", "course") if k not in row]
        if missing:
            raise ScheduleError(
                f"{source}: class row {n} is missing {', '.join(missing)}"
            )
        try:
            meetings.append(Meeting(
                normalize_day(row["day"]),
                normalize_hour(row["start"]),
                normalize_course(row["course"]),
            ))
        except ValueError as exc:
            raise ScheduleError(f"{source}: class row {n}: {exc}") from None

    return Schedule(tuple(meetings), tolerance)


def load_schedule(path: Path | None = None) -> Schedule:
    """Read the schedule file. Raises ScheduleError if missing or malformed."""
    file = Path(path) if path is not None else SCHEDULE_FILE
    if not file.exists():
        raise ScheduleError(
            f"no class schedule at {file}. Run:  intake setup"
        )
    return parse_schedule(file.read_text(), file.name)


SCHEDULE_TEMPLATE = """\
# LectureAI class schedule. One row per class meeting.
#
# day     Mon Tue Wed Thu Fri Sat Sun
# start   the hour the class begins, 24-hour clock (14 means 2pm)
# course  the code used for the Drive folder and filenames
#
# Edit this file by hand or rerun `intake setup`. Only courses listed here
# are recognized, so a stray number in a filename cannot invent a folder.

classes = [
{rows}
]

# How far a recording's START may sit from a class start and still match.
# Measured from the start, not the file mtime, so this must stay well under
# the gap between back-to-back classes: with one class at 12:00 and the next
# at 14:00 on the same day, anything near 60 makes the two windows meet and
# the wrong course wins.
tolerance_minutes = {tolerance}
"""


def render_schedule(meetings, tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES) -> str:
    """The text of a schedule file for these meetings.

    `meetings` is any iterable of Meeting or (day, hour, course) rows. Rows are
    written in weekday order so the file reads like a timetable.
    """
    normalized = []
    for row in meetings:
        if isinstance(row, Meeting):
            normalized.append(row)
        else:
            day, hour, course = row
            normalized.append(Meeting(normalize_day(day), normalize_hour(hour),
                                      normalize_course(course)))
    normalized.sort(key=lambda m: (DAYS.index(m.day), m.hour))
    width = max((len(m.course) for m in normalized), default=0)
    lines = [
        f'  {{ day = "{m.day}", start = {m.hour:>2}, '
        f'course = "{m.course}"{" " * (width - len(m.course))} }},'
        for m in normalized
    ]
    return SCHEDULE_TEMPLATE.format(rows="\n".join(lines), tolerance=tolerance_minutes)


def write_schedule(meetings, tolerance_minutes: int = DEFAULT_TOLERANCE_MINUTES,
                   path: Path | None = None) -> Path:
    """Write the schedule file and drop the cached copy."""
    file = Path(path) if path is not None else SCHEDULE_FILE
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(render_schedule(meetings, tolerance_minutes))
    reload_schedule()
    return file


_schedule_cache: Schedule | None = None


def schedule() -> Schedule:
    """The loaded schedule, read from SCHEDULE_FILE on first use."""
    global _schedule_cache
    if _schedule_cache is None:
        _schedule_cache = load_schedule()
    return _schedule_cache


def reload_schedule() -> None:
    """Forget the cached schedule so the next call re-reads the file."""
    global _schedule_cache
    _schedule_cache = None


def set_schedule(value: Schedule | dict | None) -> None:
    """Install a schedule without a file, for tests and for --course overrides.

    Accepts a Schedule, the old (day, hour) -> course dict, or None to go
    back to reading the file.
    """
    global _schedule_cache
    if isinstance(value, dict):
        value = Schedule(tuple(Meeting(d, h, c) for (d, h), c in value.items()))
    _schedule_cache = value


def courses() -> list[str]:
    """Every course code in the schedule, sorted."""
    return schedule().courses()


def __getattr__(name: str):
    # The schedule used to be two module constants. Anything still reading
    # them gets the live values from the file.
    if name == "SCHEDULE":
        return schedule().by_slot
    if name == "SCHEDULE_TOLERANCE_MINUTES":
        return schedule().tolerance_minutes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    """Map a recording to a course code from the schedule.

    Matches the class whose start is nearest the recording's start, within
    the schedule's tolerance. Pass duration_seconds whenever you have it;
    without it this falls back to comparing against the mtime, which skews
    toward the following class.
    """
    when = recording_start(file_mtime, duration_seconds)
    day = when.strftime("%a")  # "Mon", "Tue", ...
    current = schedule()

    best_course = UNKNOWN_COURSE
    best_delta = None

    for (sched_day, sched_hour), course in current.by_slot.items():
        if sched_day != day:
            continue
        start = when.replace(hour=sched_hour, minute=0, second=0, microsecond=0)
        delta_minutes = abs((when - start).total_seconds()) / 60
        if delta_minutes > current.tolerance_minutes:
            continue
        if best_delta is None or delta_minutes < best_delta:
            best_delta = delta_minutes
            best_course = course

    return best_course


def infer_course_from_filename(filename: str) -> str:
    """Pull a known course code out of a filename, or UNKNOWN_COURSE.

    Only codes that appear in the schedule are accepted, so a date or a random
    number in the name can't invent a course folder.
    """
    known = {code.upper(): code for code in courses()}
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
    sorted by date. Returns None for a course that isn't in the schedule, or
    one that doesn't meet again inside `within_days`.
    """
    if isinstance(after, str):
        try:
            after = datetime.strptime(after[:10], "%Y-%m-%d")
        except ValueError:
            return None

    meeting_days = {m.day for m in schedule().meetings if m.course == course}
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


# Settings that come from .env, with their defaults. reload() re-reads these
# so a long-running panel sees what setup just wrote without a restart.
ENV_SETTINGS = {
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "DRIVE_ROOT_FOLDER_NAME": "Lecture Notes",
    "DRIVE_PARENT_FOLDER_ID": "",
    "NOTION_TOKEN": "",
    "NOTION_DATABASE": "",
    "NOTION_VERSION": "2026-03-11",
    "NOTION_TARGET": "weekly",
    "NOTION_PROP_DUE": "",
    "NOTION_PROP_COURSE": "",
    "NOTION_PROP_KIND": "",
    "NOTION_PROP_SOURCE": "",
    "RECORD_DEVICE": "MacBook Pro Microphone",
}


def reload() -> None:
    """Re-read .env and the schedule file into this module.

    Values are taken from the file itself, not from os.environ, so a key the
    user just removed in the file actually goes away rather than lingering
    from the first load.
    """
    from dotenv import dotenv_values
    file_values = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    for name, default in ENV_SETTINGS.items():
        value = file_values.get(name)
        if value is None:
            value = os.environ.get(name, default)
        globals()[name] = value or default
    reload_schedule()


def require(name: str) -> str:
    """Fetch a required setting or fail with a readable message."""
    value = globals().get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Run:  intake setup   "
            f"(or add it to {ENV_FILE})"
        )
    return value
