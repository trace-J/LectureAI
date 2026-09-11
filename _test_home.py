"""Shared by the test scripts: a throwaway root directory, set before config
is imported, so no test can read or write the real ~/.intake. The profile is
the default one, syllabus, whatever the shell says.

    from _test_home import fresh_home
    HOME = fresh_home()            # must run before `from intake import config`
"""
import os
import tempfile
from pathlib import Path

# The schedule the tests were written against. Tuesday 14:00 is ACCT-4321,
# RELI-3304 meets once a week, and no class meets on a Sunday.
SAMPLE_SCHEDULE = """\
classes = [
  { day = "Mon", start = 9,  course = "ENTR-4306" },
  { day = "Tue", start = 12, course = "ENTR-3306" },
  { day = "Tue", start = 14, course = "ACCT-4321" },
  { day = "Wed", start = 9,  course = "ENTR-4306" },
  { day = "Thu", start = 12, course = "ENTR-3306" },
  { day = "Thu", start = 14, course = "ACCT-4321" },
  { day = "Fri", start = 9,  course = "ENTR-4306" },
  { day = "Fri", start = 12, course = "RELI-3304" },
]
tolerance_minutes = 45
"""


def fresh_home(schedule: str | None = SAMPLE_SCHEDULE) -> Path:
    """Create an empty root, point $INTAKE_HOME at it, seed the schedule.

    Returns the default profile's home inside that root, which is what
    config.HOME_DIR resolves to: <root>/syllabus.
    """
    root = Path(tempfile.mkdtemp(prefix="intake-test-"))
    os.environ["INTAKE_HOME"] = str(root)
    os.environ.pop("INTAKE_PROFILE", None)
    home = root / "syllabus"
    home.mkdir()
    if schedule is not None:
        (home / "schedule.toml").write_text(schedule)
    return home
