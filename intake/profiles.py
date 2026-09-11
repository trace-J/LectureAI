"""The two things intake can be: Syllabus, for lectures, and Sous, for client calls.

A profile is everything that differs between the two: where its data lives,
what its schedule file is called, which summary schema and prompt Claude is
given, the Drive folder it files into, how an unplaced recording is named,
where its to-dos go in Notion, and which port its panel listens on. The
pipeline itself is the same code either way and reads all of these from
``config.PROFILE`` rather than spelling any of them out.

Selection, in order: the ``--profile`` flag, then ``$INTAKE_PROFILE``, then
Syllabus. The ``syllabus`` and ``sous`` commands are the ``intake`` command
with the profile chosen.

This module must stay importable before config is, so it holds no paths and
reads no environment on import.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from intake import schemas

PROFILE_ENV_VAR = "INTAKE_PROFILE"


class ProfileError(ValueError):
    """An unknown profile name, or a --profile flag with nothing after it."""


@dataclass(frozen=True)
class Profile:
    name: str                       # what --profile and $INTAKE_PROFILE say
    home_subdir: str                # under the intake root: ~/.intake/<this>/
    schedule_filename: str          # the timetable file inside that home
    summary_schema: type[BaseModel]  # what Claude's response is constrained to
    summary_prompt: str             # the system prompt that goes with it
    drive_root_folder: str          # created in My Drive on first upload
    filename_prefix: str            # "lecture_<stamp>.m4a" when no course matched
    notion_target: str              # "weekly" or "database"; .env can override
    panel_port: int                 # the control panel's localhost port
    subject_label: str = "Course"   # what the schedule's code stands for

    @property
    def title(self) -> str:
        """The name as it is shown to a person: Syllabus, Sous."""
        return self.name.capitalize()

    @property
    def fallback_slug(self) -> str:
        """Topic slug when the model gave none: Lecture-Notes, Call-Notes."""
        return f"{self.filename_prefix.capitalize()}-Notes"


# The Drive folder keeps the name LectureAI created it under. Renaming it here
# would make the app search for a folder that does not exist and create a new
# one, leaving every summary already filed under "Lecture Notes" orphaned.
SYLLABUS = Profile(
    name="syllabus",
    home_subdir="syllabus",
    schedule_filename="schedule.toml",
    summary_schema=schemas.LectureSummary,
    summary_prompt=schemas.LECTURE_SYSTEM_PROMPT,
    drive_root_folder="Lecture Notes",
    filename_prefix="lecture",
    notion_target="weekly",
    panel_port=5173,
    subject_label="Course",
)

SOUS = Profile(
    name="sous",
    home_subdir="sous",
    schedule_filename="calls.toml",
    summary_schema=schemas.CallSummary,
    summary_prompt=schemas.CALL_SYSTEM_PROMPT,
    drive_root_folder="Client Calls",
    filename_prefix="call",
    notion_target="database",
    panel_port=5174,
    subject_label="Client",
)

PROFILES: dict[str, Profile] = {p.name: p for p in (SYLLABUS, SOUS)}
DEFAULT_PROFILE = SYLLABUS


def get(name: str) -> Profile:
    """The profile called `name`, or a ProfileError naming the real ones."""
    key = (name or "").strip().lower()
    if key not in PROFILES:
        raise ProfileError(
            f"unknown profile {name!r}; choose one of {', '.join(PROFILES)}"
        )
    return PROFILES[key]


def select(flag: str | None = None, env: dict | None = None) -> Profile:
    """--profile flag, then $INTAKE_PROFILE, then the default.

    Takes the environment as an argument so tests can resolve against a fake
    one without touching the process environment.
    """
    if flag is not None and flag.strip():
        return get(flag)
    if env is None:
        import os
        env = os.environ
    named = (env.get(PROFILE_ENV_VAR) or "").strip()
    if named:
        return get(named)
    return DEFAULT_PROFILE


def extract_flag(args: list[str]) -> tuple[str | None, list[str]]:
    """Pull `--profile NAME` or `--profile=NAME` out of an argument list.

    Accepted anywhere in the list, so `intake doctor --profile sous` works as
    well as `intake --profile sous doctor`. Returns the name (None if the flag
    is absent) and the remaining arguments.
    """
    name: str | None = None
    rest: list[str] = []
    skip = False
    for i, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--profile":
            if i + 1 >= len(args):
                raise ProfileError("--profile needs a name: " + ", ".join(PROFILES))
            name, skip = args[i + 1], True
        elif arg.startswith("--profile="):
            name = arg.split("=", 1)[1]
        else:
            rest.append(arg)
    return name, rest
