"""`intake doctor`: one line per check, pass or fail, and the exact fix.

Nothing here touches the network. Every check reads the machine and the home
directory and says what it found, so this is safe to run at any time and is
the first thing to try when something misbehaves.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from intake import config, google_client

MIN_PYTHON = (3, 11)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str          # what was found
    fix: str = ""        # what to do about it, shown only on failure
    required: bool = True


def check_python() -> Check:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    ok = (version.major, version.minor) >= MIN_PYTHON
    return Check("Python", ok, text,
                 f"needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer: brew install python@3.12, "
                 f"then reinstall with pipx")


def check_ffmpeg() -> Check:
    path = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    if path and probe:
        return Check("ffmpeg", True, path)
    return Check("ffmpeg", False, "not on PATH", "brew install ffmpeg")


def check_home() -> Check:
    home = config.HOME_DIR
    source = config.HOME_SOURCE
    if source == "default":
        where = "default"
    elif source == config.LEGACY_HOME_ENV_VAR:
        where = (f"from ${source}, the old name; set ${config.HOME_ENV_VAR} "
                 f"instead, both work for now")
    else:
        where = f"from ${source}"
    return Check("home directory", home.is_dir(), f"{home} ({where})",
                 "intake setup")


def check_key(label: str, name: str) -> Check:
    value = getattr(config, name, "")
    if value:
        shown = f"{value[:7]}...{value[-4:]}" if len(value) > 14 else "set"
        return Check(label, True, shown)
    return Check(label, False, f"{name} is not set in {config.ENV_FILE}",
                 "intake setup")


def check_schedule() -> Check:
    try:
        loaded = config.load_schedule()
    except config.ScheduleError as exc:
        fix = "intake setup" if not config.SCHEDULE_FILE.exists() \
            else f"fix the row it names in {config.SCHEDULE_FILE}, or rerun intake setup"
        return Check("class schedule", False, str(exc), fix)
    count = len(loaded.meetings)
    courses = ", ".join(loaded.courses()) or "no courses"
    if count == 0:
        return Check("class schedule", False,
                     f"{config.SCHEDULE_FILE} has no classes",
                     "intake setup, and enter at least one class")
    return Check("class schedule", True,
                 f"{count} class meeting{'s' if count != 1 else ''}, "
                 f"{courses}, tolerance {loaded.tolerance_minutes} min")


def check_drive_client() -> Check:
    try:
        installed = google_client.client_config().get("installed", {})
    except (OSError, ValueError) as exc:
        return Check("Drive OAuth client", False, f"unreadable: {exc}",
                     f"delete {config.CREDENTIALS_FILE} to fall back to the bundled client")
    if installed.get("client_id") and installed.get("client_secret"):
        return Check("Drive OAuth client", True, google_client.describe())
    return Check("Drive OAuth client", False, "client config has no client_id",
                 f"delete {config.CREDENTIALS_FILE} to fall back to the bundled client")


def check_drive_token(now: datetime | None = None) -> Check:
    token = config.TOKEN_FILE
    if not token.exists():
        return Check("Drive authorization", False, f"no {token.name} yet",
                     "intake login")
    try:
        data = json.loads(token.read_text())
    except (OSError, ValueError):
        return Check("Drive authorization", False, f"{token} is not readable JSON",
                     f"delete it and run intake login")

    scopes = set(data.get("scopes") or [])
    if scopes and scopes != set(config.DRIVE_SCOPES):
        return Check("Drive authorization", False,
                     f"token was issued for different scopes ({', '.join(sorted(scopes))})",
                     "intake login")

    if data.get("refresh_token"):
        # The access token expires hourly by design; the refresh token is what
        # keeps the watcher going, so its presence is the real test.
        return Check("Drive authorization", True,
                     f"{token.name} present with a refresh token")

    expiry_text = data.get("expiry")
    if expiry_text:
        try:
            expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            if expiry > current:
                return Check("Drive authorization", True,
                             f"access token valid until {expiry:%Y-%m-%d %H:%M} UTC "
                             f"(no refresh token; will need intake login after that)")
            return Check("Drive authorization", False,
                         f"access token expired {expiry:%Y-%m-%d %H:%M} UTC and "
                         f"there is no refresh token", "intake login")
        except ValueError:
            pass
    return Check("Drive authorization", False,
                 f"{token.name} has neither a refresh token nor a readable expiry",
                 "intake login")


NOTION_SKIP_MARKER = "# notion: skipped"


def notion_skipped() -> bool:
    """Whether setup recorded a deliberate decision not to use Notion."""
    try:
        return NOTION_SKIP_MARKER in config.ENV_FILE.read_text().lower()
    except OSError:
        return False


def check_notion() -> Check:
    token, database = config.NOTION_TOKEN, config.NOTION_DATABASE
    if token and database:
        return Check("Notion", True, f"configured, target {config.NOTION_TARGET}",
                     required=False)
    if token or database:
        missing = "NOTION_DATABASE" if token else "NOTION_TOKEN"
        return Check("Notion", False, f"half configured: {missing} is missing",
                     "intake setup, or clear both NOTION_ values to skip Notion",
                     required=False)
    if notion_skipped():
        return Check("Notion", True, "skipped in setup (optional)", required=False)
    return Check("Notion", True, "not configured (optional)",
                 required=False)


def check_microphone() -> Check:
    if shutil.which("ffmpeg") is None:
        return Check("microphone", False, "cannot check without ffmpeg",
                     "brew install ffmpeg")
    from intake import record
    devices = record.list_devices()
    if not devices:
        # The same conclusion record.py draws: no devices from a Mac that has
        # a microphone means the terminal was refused access.
        return Check("microphone", False, "ffmpeg found no audio input devices",
                     "System Settings > Privacy & Security > Microphone: enable it "
                     "for your terminal app, then rerun")
    try:
        _index, name = record.resolve_device(None)
        return Check("microphone", True,
                     f"{len(devices)} input{'s' if len(devices) != 1 else ''}, "
                     f"will record from {name}")
    except RuntimeError as exc:
        return Check("microphone", False, str(exc),
                     "intake setup and pick a microphone that is attached")


def check_legacy() -> Check | None:
    """Only reported when an older install's data has not been moved yet."""
    found = config.legacy_files()
    if not found:
        return None
    roots = " and ".join(sorted({str(config.legacy_root(p)) for p in found}))
    names = ", ".join(p.name for p in found[:4]) + (" ..." if len(found) > 4 else "")
    return Check("older install", False,
                 f"{len(found)} data file(s) still in {roots}: {names}",
                 f"intake setup (it offers to move them into {config.HOME_DIR})",
                 required=False)


def check_old_home() -> Check | None:
    """Only reported while ~/.lectureai, the home before the rename, exists.

    Before the move, check_legacy carries the files and the fix. Once the new
    home has its .env this is the line that says the move happened, or that
    the old folder still holds data nothing reads any more.
    """
    old = config.old_default_home()
    if old is None or not config.ENV_FILE.exists():
        return None
    left = config.data_files_in(old, config.LEGACY_HOME_FILES)
    if not left:
        return Check("older home", True,
                     f"{old} migrated into {config.HOME_DIR}; only scratch files "
                     f"remain there", required=False)
    return Check("older home", True,
                 f"{old} still holds {len(left)} data file(s) nothing reads now; "
                 f"{config.HOME_DIR} is the home in use", required=False)


def run_checks() -> list[Check]:
    checks = [
        check_python(),
        check_ffmpeg(),
        check_home(),
        check_key("OpenAI key", "OPENAI_API_KEY"),
        check_key("Anthropic key", "ANTHROPIC_API_KEY"),
        check_schedule(),
        check_drive_client(),
        check_drive_token(),
        check_notion(),
        check_microphone(),
    ]
    for extra in (check_legacy(), check_old_home()):
        if extra:
            checks.append(extra)
    return checks


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        mark = "ok  " if c.ok else "FAIL"
        lines.append(f"{mark}  {c.name:<{width}}  {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"{'':6}{'':{width}}  fix: {c.fix}")
    failed = [c for c in checks if not c.ok and c.required]
    optional = [c for c in checks if not c.ok and not c.required]
    lines.append("")
    if not failed and not optional:
        lines.append("Everything checks out.")
    elif not failed:
        lines.append(f"Ready to run. {len(optional)} optional item(s) need attention.")
    else:
        lines.append(f"{len(failed)} required check(s) failed. Fix those and rerun "
                     f"intake doctor.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake doctor",
        description="Check this install and say exactly how to fix what is wrong.",
    )
    parser.parse_args(argv)
    checks = run_checks()
    print(render(checks))
    return 0 if all(c.ok or not c.required for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
