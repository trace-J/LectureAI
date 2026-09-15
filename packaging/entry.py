"""What Syllabus.app runs.

Double-clicked, the app has no arguments and opens the window (intake.app).
Given arguments it is the `syllabus` command, so the one binary inside the
bundle can also be the watcher and the recorder the panel starts, and a
Terminal can run `Syllabus.app/Contents/MacOS/Syllabus doctor`.

The profile is fixed to Syllabus; --profile on the command line still wins,
as it does everywhere.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None, run_cli=None, run_app=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    from intake import profiles
    os.environ[profiles.PROFILE_ENV_VAR] = profiles.SYLLABUS.name
    if args:
        if run_cli is None:
            from intake import cli
            run_cli = cli.main
        return run_cli(args)
    if run_app is None:
        from intake import app
        run_app = app.main
    return run_app([])


if __name__ == "__main__":
    raise SystemExit(main())
