"""`lectureai setup`: the interactive first run.

Asks for the two API keys, the microphone, the class schedule, and optionally
Notion, then writes .env and schedule.toml into the home directory. Never
touches the code directory. Run it again and it shows what is there, so one
value can be changed without retyping the rest.

Every prompt goes through `ask` and every message through `say`, so the whole
flow can be driven by a test with scripted answers and a temp home.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from lectureai import config

Ask = Callable[[str], str]
Say = Callable[[str], None]

NOTION_SKIP_MARKER = "# notion: skipped in setup"

# Keys the wizard owns. Anything else already in .env is carried over
# untouched under its own heading, so a hand-added override survives a rerun.
MANAGED_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RECORD_DEVICE",
                "NOTION_TOKEN", "NOTION_DATABASE")

ENV_TEMPLATE = """\
# LectureAI settings. Written by `lectureai setup`; safe to edit by hand.
# Rerun `lectureai setup` to change one value without retyping the rest.

OPENAI_API_KEY={openai}
ANTHROPIC_API_KEY={anthropic}

# Which microphone to record from: a substring of the device name as ffmpeg
# reports it (lectureai record --list-devices). Names beat indices, which get
# reshuffled whenever a phone or headset connects.
RECORD_DEVICE={device}

# Optional. Send lecture action items to a Notion to-do database as dated
# tasks. With both unset the pipeline skips Notion entirely. Create an
# integration at notion.so/my-integrations, then share your database with it
# (database ... menu, Connections), or every call returns 404.
{notion}
"""

EXTRA_TEMPLATE = """
# Other settings kept from your previous .env.
{extras}
"""


def _quote(value: str) -> str:
    """Quote a value for .env when it has anything dotenv would misread."""
    if value == "" or any(ch in value for ch in " #'\"\\\t"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def mask(secret: str) -> str:
    """Enough of a key to recognize it, never enough to use it."""
    if not secret:
        return "not set"
    if len(secret) <= 10:
        return "set"
    return f"{secret[:6]}...{secret[-4:]}"


def render_env(values: dict[str, str], notion_skipped: bool) -> str:
    """The text of .env for these values."""
    if notion_skipped or not (values.get("NOTION_TOKEN") or values.get("NOTION_DATABASE")):
        notion = (f"{NOTION_SKIP_MARKER}\n"
                  f"# NOTION_TOKEN=ntn_...\n"
                  f"# NOTION_DATABASE=https://www.notion.so/...")
    else:
        notion = (f"NOTION_TOKEN={_quote(values.get('NOTION_TOKEN', ''))}\n"
                  f"NOTION_DATABASE={_quote(values.get('NOTION_DATABASE', ''))}")

    text = ENV_TEMPLATE.format(
        openai=_quote(values.get("OPENAI_API_KEY", "")),
        anthropic=_quote(values.get("ANTHROPIC_API_KEY", "")),
        device=_quote(values.get("RECORD_DEVICE", "")),
        notion=notion,
    )
    extras = {k: v for k, v in values.items()
              if k not in MANAGED_KEYS and v is not None}
    if extras:
        text += EXTRA_TEMPLATE.format(
            extras="\n".join(f"{k}={_quote(v)}" for k, v in sorted(extras.items()))
        )
    return text


def write_env(path: Path, values: dict[str, str], notion_skipped: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_env(values, notion_skipped))
    path.chmod(0o600)
    return path


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def parse_meeting_line(line: str) -> config.Meeting:
    """"Tue 14 ACCT-4321" -> Meeting. Commas are fine too."""
    parts = [p for p in line.replace(",", " ").split() if p]
    if len(parts) != 3:
        raise ValueError(
            "enter three things: day, start hour, course code, "
            "like  Tue 14 ACCT-4321"
        )
    day, hour, course = parts
    return config.Meeting(config.normalize_day(day), config.normalize_hour(hour),
                          config.normalize_course(course))


def _yes(answer: str, default: bool) -> bool:
    text = answer.strip().lower()
    if not text:
        return default
    return text in ("y", "yes")


class Wizard:
    def __init__(self, ask: Ask = input, say: Say = print,
                 home: Path | None = None, devices=None, allow_login: bool = True):
        self.ask = ask
        self.say = say
        self.home = Path(home) if home is not None else config.HOME_DIR
        self.env_file = self.home / ".env"
        self.schedule_file = self.home / "schedule.toml"
        self.token_file = self.home / "token.json"
        # Injected for tests; otherwise asked of ffmpeg when needed.
        self._devices = devices
        self.allow_login = allow_login

    # --- prompts ------------------------------------------------------------

    def _prompt(self, label: str, current: str = "", secret: bool = False) -> str:
        shown = mask(current) if secret else current
        suffix = f" [{shown}]" if current else ""
        try:
            answer = self.ask(f"{label}{suffix}: ").strip()
        except EOFError:
            answer = ""
        return answer or current

    def ask_keys(self, values: dict[str, str]) -> None:
        self.say("")
        self.say("API keys. Paste each one; Enter keeps the value shown.")
        values["OPENAI_API_KEY"] = self._prompt(
            "OpenAI API key (platform.openai.com/api-keys)",
            values.get("OPENAI_API_KEY", ""), secret=True)
        values["ANTHROPIC_API_KEY"] = self._prompt(
            "Anthropic API key (console.anthropic.com)",
            values.get("ANTHROPIC_API_KEY", ""), secret=True)

    def devices(self) -> list[tuple[int, str]]:
        if self._devices is not None:
            return self._devices
        if shutil.which("ffmpeg") is None:
            return []
        from lectureai import record
        return record.list_devices()

    def ask_microphone(self, values: dict[str, str]) -> None:
        self.say("")
        current = values.get("RECORD_DEVICE", "")
        devices = self.devices()
        if not devices:
            if shutil.which("ffmpeg") is None:
                self.say("Microphone: ffmpeg is not installed yet (brew install ffmpeg), "
                         "so the list is empty. Keeping the default for now.")
            else:
                self.say("Microphone: ffmpeg found no audio inputs. If this Mac has "
                         "one, grant your terminal microphone access in System "
                         "Settings > Privacy & Security. Keeping the default for now.")
            values["RECORD_DEVICE"] = current or config.RECORD_DEVICE
            return

        self.say("Microphone. Pick a number, or Enter to keep the current one.")
        for index, name in devices:
            marker = "   <- current" if current and current.lower() in name.lower() else ""
            self.say(f"  [{index}] {name}{marker}")
        if not current:
            # The same preference record.py applies: the built-in mic by
            # name, never whatever happens to be index 0.
            for candidate in (config.RECORD_DEVICE, *config.RECORD_DEVICE_FALLBACKS):
                hit = next((n for _i, n in devices if candidate.lower() in n.lower()), None)
                if hit:
                    current = hit
                    break
            current = current or devices[0][1]
        while True:
            answer = self._prompt("Microphone", current)
            if answer.isdigit():
                match = next((n for i, n in devices if i == int(answer)), None)
                if match:
                    values["RECORD_DEVICE"] = match
                    return
                self.say(f"  no device numbered {answer}")
                continue
            if any(answer.lower() in n.lower() for _i, n in devices):
                values["RECORD_DEVICE"] = answer
                return
            self.say(f"  nothing attached matches {answer!r}; try a number from the list")

    def ask_schedule(self) -> tuple[list[config.Meeting], int]:
        self.say("")
        existing: config.Schedule | None = None
        if self.schedule_file.exists():
            try:
                existing = config.load_schedule(self.schedule_file)
            except config.ScheduleError as exc:
                self.say(f"Your current schedule file could not be read: {exc}")

        if existing and existing.meetings:
            self.say("Class schedule on file:")
            for m in sorted(existing.meetings,
                            key=lambda m: (config.DAYS.index(m.day), m.hour)):
                self.say(f"  {m.day} {m.hour:>2}:00  {m.course}")
            try:
                keep = self.ask("Keep this schedule? [Y/n] ")
            except EOFError:
                keep = ""
            if _yes(keep, default=True):
                return list(existing.meetings), existing.tolerance_minutes

        tolerance = existing.tolerance_minutes if existing else config.DEFAULT_TOLERANCE_MINUTES
        self.say("Class schedule. One meeting per line as: day, start hour "
                 "(24-hour clock), course code.")
        self.say("  For example:  Tue 14 ACCT-4321     Enter a blank line when done.")
        meetings: list[config.Meeting] = []
        while True:
            try:
                line = self.ask(f"  class {len(meetings) + 1}: ").strip()
            except EOFError:
                line = ""
            if not line:
                if meetings:
                    break
                self.say("  at least one class is needed for recordings to be filed")
                try:
                    again = self.ask("  add one now? [Y/n] ")
                except EOFError:
                    again = "n"
                if _yes(again, default=True):
                    continue
                break
            try:
                meetings.append(parse_meeting_line(line))
            except ValueError as exc:
                self.say(f"  {exc}")
        return meetings, tolerance

    def ask_notion(self, values: dict[str, str]) -> bool:
        """Returns True if Notion was deliberately skipped."""
        self.say("")
        token, database = values.get("NOTION_TOKEN", ""), values.get("NOTION_DATABASE", "")
        if token and database:
            self.say(f"Notion is set up (token {mask(token)}).")
            try:
                keep = self.ask("Keep it? [Y/n] ")
            except EOFError:
                keep = ""
            if _yes(keep, default=True):
                return False
        else:
            self.say("Notion is optional: each deadline a lecture mentions becomes a "
                     "task in a to-do database.")
            try:
                want = self.ask("Set up Notion now? [y/N] ")
            except EOFError:
                want = ""
            if not _yes(want, default=False):
                values["NOTION_TOKEN"] = ""
                values["NOTION_DATABASE"] = ""
                return True

        self.say("  Create an internal integration at notion.so/my-integrations, then "
                 "share your to-do database with it (... menu > Connections).")
        values["NOTION_TOKEN"] = self._prompt("  Notion integration secret", token, secret=True)
        values["NOTION_DATABASE"] = self._prompt("  Notion database URL", database)
        if not (values["NOTION_TOKEN"] and values["NOTION_DATABASE"]):
            self.say("  Both are needed; leaving Notion off for now.")
            values["NOTION_TOKEN"] = ""
            values["NOTION_DATABASE"] = ""
            return True
        return False

    def offer_login(self) -> None:
        if not self.allow_login:
            return
        self.say("")
        if self.token_file.exists():
            self.say("Google Drive is already authorized.")
            return
        try:
            answer = self.ask("Authorize Google Drive now? It opens a browser. [Y/n] ")
        except EOFError:
            answer = "n"
        if not _yes(answer, default=True):
            self.say("Skipped. Run `lectureai login` before the first recording.")
            return
        from lectureai import upload
        try:
            upload.get_credentials(interactive=True)
            self.say("Drive authorized.")
        except Exception as exc:  # the flow reports its own detail
            self.say(f"Drive authorization did not finish: {exc}")
            self.say("Run `lectureai login` to try again.")

    # --- the run ------------------------------------------------------------

    def run(self) -> int:
        self.say(f"LectureAI setup. Settings go in {self.home}")
        if self.env_file.exists():
            self.say("Existing settings found; Enter keeps any value shown in brackets.")

        if config.legacy_files() and self.home == config.HOME_DIR:
            from lectureai import cli
            self.say("")
            cli.offer_migration(ask=self.ask, say=self.say)

        values = read_env(self.env_file)
        self.ask_keys(values)
        self.ask_microphone(values)
        meetings, tolerance = self.ask_schedule()
        notion_skipped = self.ask_notion(values)

        self.home.mkdir(parents=True, exist_ok=True)
        for sub in ("inbox", "processed", ".work"):
            (self.home / sub).mkdir(exist_ok=True)
        write_env(self.env_file, values, notion_skipped)
        if meetings:
            config.write_schedule(meetings, tolerance, path=self.schedule_file)
        elif not self.schedule_file.exists():
            self.say("No schedule written. Recordings cannot be filed until you add one.")

        self.say("")
        self.say(f"Wrote {self.env_file}")
        if meetings:
            self.say(f"Wrote {self.schedule_file} ({len(meetings)} class meetings)")

        self.offer_login()

        self.say("")
        self.say("Next:  lectureai doctor    to confirm everything is in place")
        self.say("       lectureai record    to record a lecture")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lectureai setup",
        description="Interactive first run: API keys, microphone, class schedule.",
    )
    parser.add_argument("--no-login", action="store_true",
                        help="do not offer to authorize Google Drive at the end")
    args = parser.parse_args(argv)
    try:
        return Wizard(allow_login=not args.no_login).run()
    except KeyboardInterrupt:
        print("\nsetup canceled; nothing was changed unless a 'Wrote' line "
              "appeared above", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
