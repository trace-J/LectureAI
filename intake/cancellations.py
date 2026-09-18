"""Classes that did not meet, so a missed lecture is not counted as one.

The dashboard reads the schedule as a promise: every meeting on it should
end up with a recording filed, and one that does not breaks the streak and
drags the coverage rate down. That is right for a class somebody slept
through and wrong for a class that was called off. The professor was sick,
the campus closed, the exam replaced the lecture. Nothing was missed, so
nothing should look missed.

This file is the correction. It names class meetings that did not happen,
one row per (course, class date), and insights.py drops those slots from
the streak, from the coverage counts, and from what the week grid calls a
miss. It is the only stored state behind the dashboard: everything else is
computed from pipeline.log and the schedule, so a row removed here puts the
meeting straight back on the books.

Only meetings the schedule actually has can be listed, and only ones with
no recording filed against them; the panel's routes check both before
calling cancel(). A recording that arrives afterward wins on its own, in
insights.py, without this file being touched.
"""

from __future__ import annotations

import json
import os
from datetime import date

from intake import config

# One row per canceled meeting, and a student has a few hundred meetings in
# a semester. A file far past that is not a schedule any more, so reading
# stops rather than letting a runaway writer turn every poll into a scan.
MAX_ROWS = 2000

# What a row carries: which meeting, why (optional, the student's own
# words), and when it was marked. The note is shown back on the tile.
MAX_NOTE = 200


class CancelError(ValueError):
    """The cancellation could not be recorded, with a reason to show."""


def clean_date(value: object) -> str:
    """A class date as YYYY-MM-DD, or a refusal naming what was wrong."""
    if not isinstance(value, str):
        raise CancelError("a class date must be text, as YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise CancelError(f"{value.strip()!r} is not a date, as YYYY-MM-DD") from None


def _clean_note(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CancelError("a note must be text")
    return " ".join(value.split())[:MAX_NOTE]


def _row(raw: object) -> dict | None:
    """One stored row, or None if it is not one we wrote."""
    if not isinstance(raw, dict):
        return None
    course = raw.get("course")
    if not isinstance(course, str) or not course.strip():
        return None
    try:
        day = clean_date(raw.get("date"))
    except CancelError:
        return None
    return {
        "course": config.safe_course(course),
        "date": day,
        "note": _clean_note(raw.get("note")) if isinstance(raw.get("note"), str) else "",
        "when": raw.get("when") if isinstance(raw.get("when"), str) else "",
    }


def load() -> list[dict]:
    """Every canceled meeting on file, oldest first.

    A missing file is an empty list, and so is a damaged one: the dashboard
    showing a broken streak is a far smaller failure than the dashboard not
    drawing at all, which is what an exception here would cost.
    """
    try:
        text = config.CANCELED_FILE.read_text()
    except OSError:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    rows = []
    for raw in data[:MAX_ROWS]:
        row = _row(raw)
        if row is not None:
            rows.append(row)
    return rows


def keys(rows: list[dict] | None = None) -> set[tuple[str, str]]:
    """The (course, class date) pairs to skip, the shape insights.py wants."""
    return {(r["course"], r["date"]) for r in (load() if rows is None else rows)}


def save(rows: list[dict]) -> None:
    """Replace the file with `rows`, all at once.

    Written to a neighbor and renamed over the top, so a panel that dies
    mid-write leaves the previous list intact rather than half a JSON
    document that load() would then read as nothing at all.
    """
    config.ensure_home()
    path = config.CANCELED_FILE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rows[-MAX_ROWS:], indent=2) + "\n")
    os.replace(tmp, path)


def cancel(course: str, day: str, note: str = "", when: str = "") -> list[dict]:
    """Record that `course` did not meet on `day`. Repeating it is harmless."""
    course = config.safe_course(course)
    day = clean_date(day)
    note = _clean_note(note)
    rows = [r for r in load() if (r["course"], r["date"]) != (course, day)]
    rows.append({"course": course, "date": day, "note": note, "when": when})
    save(rows)
    return rows


def restore(course: str, day: str) -> list[dict]:
    """Put the meeting back on the books. Unknown pairs are left alone."""
    course = config.safe_course(course)
    day = clean_date(day)
    rows = [r for r in load() if (r["course"], r["date"]) != (course, day)]
    save(rows)
    return rows
