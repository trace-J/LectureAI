"""The `lectureai` command: one entry point over every module.

    lectureai setup            first run: keys, microphone, schedule
    lectureai doctor           check the install and say how to fix it
    lectureai record           record from this Mac's microphone
    lectureai watch            process the inbox until Ctrl-C
    lectureai watch --once F   process one file and exit
    lectureai panel            the control panel, and setup, in your browser
    lectureai login            authorize Google Drive
    lectureai notion --check   what the Notion integration sees
    lectureai notion --setup   add the Notion properties it needs

Every subcommand hands its remaining arguments to the module it wraps, so
`lectureai record --minutes 80` is the same as `python record.py --minutes 80`
from a checkout.
"""

from __future__ import annotations

import shutil
import sys

from lectureai import __version__, config

USAGE = __doc__.split("\n\n")[1]

COMMANDS = ("setup", "doctor", "record", "watch", "panel", "login", "notion")

# Subcommands that cannot do anything useful without a schedule, so they fail
# up front with one readable line rather than a traceback from deep inside.
NEEDS_SCHEDULE = {"record", "watch", "panel"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def offer_migration(ask=input, say=log) -> bool:
    """Move an old install's data files into the home directory, if asked.

    Runs when the home directory has no .env yet and the checkout this code
    lives in still has one next to it, which is exactly what an install from
    before the home directory existed looks like. Returns True if files moved.
    """
    found = config.legacy_files()
    if not found:
        return False

    say(f"Found data from an older install next to the code in {config.CODE_ROOT}:")
    for path in found:
        say(f"  {path.relative_to(config.CODE_ROOT)}")
    say(f"LectureAI now keeps its data in {config.HOME_DIR}.")
    try:
        answer = ask("Move these files there now? [Y/n] ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("", "y", "yes"):
        say("Left them where they are. Run `lectureai setup` to start fresh, or "
            "move them by hand.")
        return False

    config.ensure_home()
    for path in found:
        relative = path.relative_to(config.CODE_ROOT)
        destination = config.HOME_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            say(f"  kept existing {relative} in the home directory; "
                f"left the old copy in place")
            continue
        shutil.move(str(path), str(destination))
        say(f"  moved {relative}")
    say("Done. Restart any watcher or panel that was already running.")
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help", "help"):
        print(f"usage: lectureai <command> [options]\n\n{USAGE}\n\n"
              f"Data lives in {config.HOME_DIR} (set $LECTUREAI_HOME to move it).")
        return 0
    if args[0] in ("-V", "--version", "version"):
        print(f"lectureai {__version__}")
        return 0

    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        log(f"lectureai: unknown command {command!r}. "
            f"Commands: {', '.join(COMMANDS)}")
        return 2

    # An older checkout with its data next to the code: offer the move once,
    # before any command goes looking for keys it will not find. Only when a
    # person is at the keyboard; a watcher started by the panel must not hang
    # on a prompt nobody sees.
    if command != "setup" and sys.stdin.isatty() and config.legacy_files():
        if offer_migration():
            config.reload()

    if command in NEEDS_SCHEDULE and "--list-devices" not in rest \
            and "--no-browser" not in rest and command != "panel" \
            and "-h" not in rest and "--help" not in rest:
        try:
            config.schedule()
        except config.ScheduleError as exc:
            log(f"error: {exc}")
            return 1

    if command == "setup":
        from lectureai import setup_wizard
        return setup_wizard.main(rest)
    if command == "doctor":
        from lectureai import doctor
        return doctor.main(rest)
    if command == "record":
        from lectureai import record
        return record.main(rest)
    if command == "watch":
        from lectureai import watch
        return watch.main(rest)
    if command == "panel":
        from lectureai import gui
        return gui.main(rest)
    if command == "login":
        from lectureai import upload
        return upload.main(["--login", *rest])
    if command == "notion":
        from lectureai import notion_tasks
        return notion_tasks.main(rest)
    return 2  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
