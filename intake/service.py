"""Keep the control panel running, as a launchd agent for the signed-in user.

    intake service install     start the panel now, and again at every login
    intake service status      is it running, and where its log is
    intake service restart     pick up new code or a changed .env
    intake service uninstall   stop it and remove the agent

Without this, the panel lives exactly as long as the terminal (or the app
window) that started it, and every restart is a trip back to the keyboard.
launchd starts it at login, restarts it if it dies, and keeps it out of any
terminal. Each profile gets its own agent, since each has its own port.

Only the panel is kept alive. The watcher spends API credit and writes to
Drive and Notion, so starting it stays a deliberate click in the panel.

A launchd agent is what macOS has for a program that belongs to one person's
login session, which this is: it records from this Mac's microphone, so it
cannot run anywhere else. The first recording after installing will ask for
microphone permission on behalf of Python, the program the agent runs; grant
it once and it sticks.
"""

from __future__ import annotations

import os
import plistlib
import socket
import subprocess
import sys
import time
from pathlib import Path

from intake import config

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Executables the panel and the pipeline reach for: ffmpeg from Homebrew on
# either kind of Mac, then the system. launchd gives an agent almost no PATH
# of its own, so this has to be spelled out.
AGENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def label(profile=None) -> str:
    """The agent's launchd label, one per profile."""
    profile = profile or config.PROFILE
    return f"com.maincoursemedia.{profile.name}.panel"


def plist_path(profile=None, agents_dir: Path = AGENTS_DIR) -> Path:
    return agents_dir / f"{label(profile)}.plist"


def domain() -> str:
    """launchd's name for this login session."""
    return f"gui/{os.getuid()}"


def program(profile=None) -> list[str]:
    """The command the agent runs: this interpreter, this package, this profile.

    The interpreter is the one running now, so a venv install keeps using
    its venv and a pipx install its own. `-m intake.cli` rather than the
    `intake` script, which may not be on any PATH launchd knows about.
    """
    profile = profile or config.PROFILE
    return [sys.executable, "-m", "intake.cli", "--profile", profile.name,
            "panel", "--no-browser"]


def plist(profile=None) -> dict:
    """The agent definition, as the dict plistlib writes."""
    profile = profile or config.PROFILE
    env = {"PATH": AGENT_PATH, "INTAKE_PROFILE": profile.name}
    # A relocated home has to travel with the agent, or the panel would start
    # against an empty default home and open on the setup page.
    for var in (config.HOME_ENV_VAR, config.LEGACY_HOME_ENV_VAR):
        if os.environ.get(var):
            env[var] = os.environ[var]
    log = str(config.HOME_DIR / "panel.log")
    return {
        "Label": label(profile),
        "ProgramArguments": program(profile),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        # A user-facing program, so macOS treats its permission prompts
        # (microphone) as belonging to the person at the screen.
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "WorkingDirectory": str(config.HOME_DIR),
    }


def _launchctl(args: list[str], run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["launchctl", *args], capture_output=True, text=True)


def port_answers(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(port: int, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if port_answers(port):
            return True
        time.sleep(0.25)
    return port_answers(port)


def install(say=print, run=subprocess.run, agents_dir: Path = AGENTS_DIR,
            wait: float = 10.0) -> int:
    """Write the agent and start it. Re-running replaces it in place."""
    profile = config.PROFILE
    config.ensure_home()
    path = plist_path(profile, agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist(profile)))
    # Unload any earlier copy first; bootstrap refuses to load a label twice.
    _launchctl(["bootout", f"{domain()}/{label(profile)}"], run)
    result = _launchctl(["bootstrap", domain(), str(path)], run)
    if result.returncode != 0:
        say(f"launchctl could not load the agent: {result.stderr.strip() or result.stdout.strip()}")
        return 1
    _launchctl(["kickstart", "-k", f"{domain()}/{label(profile)}"], run)
    say(f"{profile.title} panel installed as {label(profile)}")
    say(f"  starts at login, restarts if it dies, log in {config.HOME_DIR / 'panel.log'}")
    if wait and not wait_for_port(profile.panel_port, wait):
        say(f"  it has not answered on port {profile.panel_port} yet; "
            f"check the log if it stays that way")
        return 1
    if wait:
        say(f"  answering at http://127.0.0.1:{profile.panel_port}")
    return 0


def uninstall(say=print, run=subprocess.run, agents_dir: Path = AGENTS_DIR) -> int:
    profile = config.PROFILE
    path = plist_path(profile, agents_dir)
    _launchctl(["bootout", f"{domain()}/{label(profile)}"], run)
    if path.exists():
        path.unlink()
        say(f"{profile.title} panel agent removed; the panel is stopped")
    else:
        say(f"no {profile.title} panel agent was installed")
    return 0


def restart(say=print, run=subprocess.run, agents_dir: Path = AGENTS_DIR) -> int:
    profile = config.PROFILE
    if not plist_path(profile, agents_dir).exists():
        say(f"no {profile.title} panel agent is installed; run `intake service install`")
        return 1
    result = _launchctl(["kickstart", "-k", f"{domain()}/{label(profile)}"], run)
    if result.returncode != 0:
        say(f"launchctl could not restart it: {result.stderr.strip() or result.stdout.strip()}")
        return 1
    say(f"{profile.title} panel restarted")
    return 0


def status(say=print, run=subprocess.run, agents_dir: Path = AGENTS_DIR) -> int:
    profile = config.PROFILE
    installed = plist_path(profile, agents_dir).exists()
    result = _launchctl(["print", f"{domain()}/{label(profile)}"], run)
    loaded = result.returncode == 0
    pid = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            pid = stripped.split("=", 1)[1].strip()
    answering = port_answers(profile.panel_port)
    say(f"{profile.title} panel agent: "
        f"{'installed' if installed else 'not installed'}, "
        f"{'loaded' if loaded else 'not loaded'}"
        f"{f', pid {pid}' if pid else ''}")
    say(f"  port {profile.panel_port}: {'answering' if answering else 'not answering'}")
    say(f"  log: {config.HOME_DIR / 'panel.log'}")
    return 0 if (installed and loaded and answering) else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "status"
    if action in ("-h", "--help", "help"):
        print(__doc__.split("\n\n")[1])
        return 0
    if action == "install":
        return install()
    if action == "uninstall":
        return uninstall()
    if action == "restart":
        return restart()
    if action == "status":
        return status()
    print(f"intake service: unknown action {action!r}; "
          f"use install, status, restart, or uninstall", file=sys.stderr)
    return 2
