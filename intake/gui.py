"""A small local control panel for the pipeline.

    intake panel          # opens http://127.0.0.1:5173 in your browser

The port is the profile's: 5173 for Syllabus, 5174 for Sous, so both panels
can run at once. --port overrides it.

Start and stop a recording, see whether the watcher is running, and check on
recent lectures, without remembering any commands. It drives the same modules
the CLI does, so anything started here behaves identically to the CLI.

The Setup page (/setup) is the browser version of `intake setup`: keys,
microphone, class schedule, Notion, and the Google Drive login, writing the
same .env and schedule.toml into the home directory. The panel opens it
first when nothing is configured yet.

Bound to localhost on purpose. It can start and stop processes and read your
pipeline log, none of which should be reachable from the network.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

from intake import account, config, doctor, insights, relay, service, setup_wizard, signin, sync
from intake import updates
from intake import notion_tasks
from intake import record as recording
from intake import transcribe

app = Flask(__name__)

# One recorder for the process. The GUI is single-user by construction, and a
# second concurrent recording would fight over the microphone anyway.
_recorder: recording.Recorder | None = None

# What the desktop app (app.py) registers when this panel runs inside it:
# "show" brings its window forward. Empty under `intake panel`, and the
# status answer says so, which is how a second copy of the app knows whether
# to ask for the window or to be one.
window_hooks: dict[str, object] = {}

WATCHER_LOG = config.WORK_DIR / "watcher-gui.log"

# Watchers this panel started. They run detached and are meant to outlive
# us, but while we are alive we are still their parent, and a child nobody
# waits on stays a zombie: kill(pid, 0) keeps saying it is alive, and the
# panel keeps reporting a watcher that died hours ago. Polling them here is
# what reaps them.
_children: list[subprocess.Popen] = []


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _current_recorder() -> recording.Recorder | None:
    """The recording in progress, whoever started it.

    ffmpeg runs in its own session and outlives this panel, so a recording
    the previous panel started is still going when this one opens. Showing
    "Not recording" then is a lie with a cost: the person starts a second
    recording, or assumes the lecture is lost. The state file record.py keeps
    is how we find and take over that ffmpeg. This is also where a recording
    that ended on its own (ffmpeg's time cap) gets filed into inbox/.
    """
    global _recorder
    if _recorder is not None and not _recorder.is_recording:
        finished, _recorder = _recorder, None
        try:
            filed = finished.finish()
            if filed is None:
                _say("recording was stopped and filed from elsewhere")
            else:
                _say(f"recording ended on its own; filed {filed.name}")
        except RuntimeError as exc:
            _say(f"recording ended on its own: {exc}")
    if _recorder is None:
        filed = recording.finish_abandoned()
        if filed is not None:
            _say(f"filed an earlier recording nobody stopped: {filed.name}")
        _recorder = recording.Recorder.adopt()
        if _recorder is not None:
            _say(f"picking up a recording already running since "
                 f"{_recorder.started:%H:%M} (pid {_recorder._proc.pid})")
    return _recorder


def _reap_children() -> None:
    """Collect exit statuses of watchers we started that have since ended."""
    for child in list(_children):
        if child.poll() is not None:
            _children.remove(child)


def _watcher_pid() -> int | None:
    """PID of the running watcher, or None.

    Decided by whether the lock is held, not by whether the pid answers: a
    watcher that was killed answers kill(0) as a zombie until its parent
    (this panel, when it started the watcher) reaps it, and a stale pid can
    be reused by an unrelated process. The flock goes away the instant the
    watcher does, whatever state its process record is in.
    """
    _reap_children()
    return config.watcher_pid()


def _courses() -> list[str]:
    """Every course in the schedule, or none while there is no schedule yet."""
    try:
        return config.courses()
    except config.ScheduleError:
        return []


def _current_class() -> str | None:
    """The course scheduled right now, so the button can name it."""
    try:
        course = config.infer_course(datetime.now())
    except config.ScheduleError:
        return None
    return None if course == config.UNKNOWN_COURSE else course


def _configured() -> bool:
    """Whether the pipeline can run at all: both keys and a readable schedule."""
    return bool(config.OPENAI_API_KEY and config.ANTHROPIC_API_KEY and _courses())


def _inbox() -> list[dict]:
    """Recordings waiting to be processed."""
    items = []
    if not config.INBOX_DIR.is_dir():
        return items
    for path in sorted(config.INBOX_DIR.iterdir()):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and not path.name.startswith("."):
            items.append({"name": path.name, "bytes": path.stat().st_size})
    return items


def _log_rows() -> list[dict]:
    """Every lecture in pipeline.log, oldest first. See insights.parse_log."""
    if not config.LOG_FILE.exists():
        return []
    return insights.parse_log(config.LOG_FILE.read_text())


def _recent(limit: int = 12) -> list[dict]:
    """Recent lectures, newest first, parsed out of pipeline.log."""
    return list(reversed(_log_rows()))[:limit]


def _insights() -> dict:
    """The dashboard's tiles and charts: the log counted against the schedule."""
    try:
        schedule = config.schedule()
    except config.ScheduleError:
        schedule = None
    return insights.compute(_log_rows(), schedule)


def _processing(watcher_pid: int | None) -> dict | None:
    """What the watcher is working on right now, or None if it is idle.

    Tied to the live watcher's pid. A watcher killed mid-lecture leaves its
    status file behind, and reporting that stale stage forever would be worse
    than saying nothing.
    """
    if watcher_pid is None or not config.STATUS_FILE.exists():
        return None
    try:
        data = json.loads(config.STATUS_FILE.read_text())
    except (OSError, ValueError):
        return None
    if data.get("pid") != watcher_pid:
        return None

    started = data.get("started", "")
    try:
        elapsed = (datetime.now() - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        elapsed = 0
    return {
        "stage": data.get("stage", ""),
        "file": data.get("file", ""),
        "course": data.get("course", ""),
        "detail": data.get("detail", ""),
        "elapsed": round(max(0.0, elapsed)),
    }


def _drive_via_account_cached() -> bool:
    """Whether the account's Drive grant was connected the last time anyone
    asked. File only: the status poll never talks to the service."""
    return bool(account.enabled() and account.load() is not None
                and account.drive_cached().get("connected"))


def _integrations() -> dict:
    """Which pieces are configured. Presence only; no secrets leave here."""
    return {
        "openai": bool(config.OPENAI_API_KEY),
        "anthropic": bool(config.ANTHROPIC_API_KEY),
        "drive": config.TOKEN_FILE.exists() or _drive_via_account_cached(),
        "notion": notion_tasks.enabled(),
    }


# --- The gate, when the panel is reached from elsewhere ------------------------
#
# The panel has no accounts of its own. Through the account service's relay
# (relay.py) the service is the sign-in and names the viewer; through a
# Cloudflare Tunnel, signin.py sends the browser to the service to sign in.
# Its gate runs before every request and sets g.viewer to who is looking.
# Requests straight from this Mac carry neither mark and are not gated, so
# the panel keeps working locally regardless.
signin.install(app)


def _base() -> str:
    """The path this panel is published under: "" here, "/p/<device>" through
    the relay, where the pages write every link and request. request.script_root
    is the WSGI name for it, set by relay.serve."""
    return request.script_root or ""


@app.get("/")
def index():
    return render_template("index.html", profile=config.PROFILE, base=_base())


@app.get("/setup")
def setup_page():
    return render_template("setup.html", profile=config.PROFILE, base=_base())


@app.get("/api/status")
def status():
    pid = _watcher_pid()
    rec = _current_recorder()
    active = rec is not None and rec.is_recording
    return jsonify({
        "recording": {
            "active": active,
            "elapsed": round(rec.elapsed, 1) if active else 0,
            "device": rec.device_name if active else "",
            "planned": rec.planned_name if active else "",
            "bytes": rec.staged_bytes if active else 0,
            "course": (rec.course or "") if active else "",
            # Started by an earlier panel and picked up by this one.
            "resumed": rec.adopted if active else False,
            # Open for a while with nothing on disk: the timer is ticking but
            # the mic is delivering nothing, and the page must say so.
            "stalled": rec.stalled if active else False,
            "warning": rec.warning if active else "",
        },
        "watcher": {"running": pid is not None, "pid": pid},
        "processing": _processing(pid),
        "inbox": _inbox(),
        "recent": _recent(),
        # What the tiles, the week grid, and the charts are drawn from.
        "insights": _insights(),
        "integrations": _integrations(),
        "courses": _courses(),
        "now_class": _current_class(),
        "configured": _configured(),
        # Who is looking, when the request came through the relay or the tunnel.
        "signed_in_as": g.get("viewer", ""),
        # Through the relay the sign-in is the account service's, so its
        # sign-out is there; through the tunnel it is this panel's own.
        "signout_url": (account.url() + "/logout") if g.get("relayed") else "/logout",
        # The panel's place on the web, and whether the socket to it is up.
        "relay": relay.status(),
        # The Syllabus account this Mac is claimed into, if any. File only;
        # the status poll never talks to the account service.
        "account": account.summary(),
        # Whether this panel runs inside the desktop app, which has a window
        # to show (see window_hooks).
        "window": "show" in window_hooks,
        # Whether a newer Syllabus has been released, from the file the daily
        # check writes (updates.py). Never the network.
        "update": updates.status(),
    })


@app.post("/api/window/show")
def window_show():
    """Bring the desktop app's window forward. A second copy of the app asks
    this instead of opening a window of its own."""
    hook = window_hooks.get("show")
    if hook is None:
        return jsonify({"ok": False, "error": "this panel has no window"}), 409
    hook()
    return jsonify({"ok": True})


# --- Start at login ------------------------------------------------------------

@app.get("/api/login-item")
def login_item_state():
    """Whether a launch agent starts this program at login (service.py)."""
    return jsonify(_login_item())


def _login_item() -> dict:
    return {
        "installed": service.installed(),
        # An agent from a different install: the Terminal install's panel
        # while the app is running, or the other way round.
        "other": service.installed() and not service.runs_this_program(),
        "label": (f"Start {config.PROFILE.title} when I log in" if "show" in window_hooks
                  else f"Start the {config.PROFILE.title} panel when I log in"),
    }


@app.post("/api/login-item")
def login_item_set():
    payload = request.get_json(silent=True) or {}
    said: list[str] = []
    if payload.get("enabled"):
        rc = service.install(say=said.append, wait=0)
    else:
        rc = service.uninstall(say=said.append)
    if rc != 0:
        return jsonify({"ok": False, "error": " ".join(said) or "launchctl refused",
                        **_login_item()}), 500
    return jsonify({"ok": True, **_login_item()})


@app.post("/api/record/start")
def record_start():
    global _recorder
    if _current_recorder() is not None:
        return jsonify({"ok": False, "error": "already recording"}), 409

    payload = request.get_json(silent=True) or {}
    course = (payload.get("course") or "").strip() or None
    if course and course not in set(_courses()):
        return jsonify({"ok": False, "error": f"unknown course {course}"}), 400

    try:
        _recorder = recording.Recorder(course=course)
        _recorder.start()
    except Exception as exc:
        _recorder = None
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "device": _recorder.device_name,
                    "planned": _recorder.planned_name})


@app.post("/api/record/stop")
def record_stop():
    global _recorder
    rec = _current_recorder()
    if rec is None:
        return jsonify({"ok": False, "error": "not recording"}), 409
    try:
        destination = rec.stop()
    except Exception as exc:
        _recorder = None
        return jsonify({"ok": False, "error": str(exc)}), 500
    wall = rec.elapsed
    _recorder = None

    # Report the audio the file actually holds, not how long the button was
    # held down. Opening the capture device costs a second or more, and on a
    # cold mic considerably more, so the wall clock overstates short takes.
    captured = transcribe.duration_seconds(destination) or 0
    return jsonify({
        "ok": True,
        "name": destination.name,
        "bytes": destination.stat().st_size,
        "seconds": round(captured, 1),
        "lost": round(max(0.0, wall - captured), 1),
        "watching": _watcher_pid() is not None,
    })


@app.post("/api/watcher/start")
def watcher_start():
    if _watcher_pid() is not None:
        return jsonify({"ok": False, "error": "already running"}), 409
    WATCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    handle = WATCHER_LOG.open("a")
    # start_new_session so the watcher outlives this GUI: closing the control
    # panel should not abandon a lecture that is midway through transcription.
    #
    # config.program spells the command the way this process was started:
    # this interpreter and the package from a checkout or a pipx install, the
    # app's own binary inside Syllabus.app.
    child = subprocess.Popen(
        watcher_command(),
        cwd=str(config.program_cwd()), stdin=subprocess.DEVNULL,
        stdout=handle, stderr=handle, start_new_session=True,
    )
    _children.append(child)
    # A watcher that exits within its first moments never got going: it
    # could not take the lock, or preflight refused it. Saying "started"
    # anyway left the button looking dead, with the reason only in a log
    # nobody was reading.
    if _exited_early(child):
        _children.remove(child)
        return jsonify({"ok": False, "error": _last_error_line(WATCHER_LOG)}), 500
    return jsonify({"ok": True})


def watcher_command() -> list[str]:
    """The command that starts the watcher for this profile."""
    return config.program("watch")


def _exited_early(child: subprocess.Popen, seconds: float = 1.5) -> bool:
    """Whether the child ended within `seconds` of being started."""
    try:
        child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _last_error_line(log: Path) -> str:
    """The most recent error the watcher logged, or a generic message."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines[-20:]):
        if "error:" in line:
            return line.split("error:", 1)[1].strip()
    return "the watcher exited right after starting; see watcher-gui.log"


@app.post("/api/watcher/stop")
def watcher_stop():
    pid = _watcher_pid()
    if pid is None:
        return jsonify({"ok": False, "error": "not running"}), 409
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


# --- Setup page -------------------------------------------------------------

_drive_login = {"running": False, "error": ""}


def _schedule_rows() -> tuple[list[dict], int, str]:
    """The schedule as the page edits it, plus any problem reading the file."""
    if not config.SCHEDULE_FILE.exists():
        return [], config.DEFAULT_TOLERANCE_MINUTES, ""
    try:
        loaded = config.load_schedule()
    except config.ScheduleError as exc:
        return [], config.DEFAULT_TOLERANCE_MINUTES, str(exc)
    rows = [{"day": m.day, "start": m.hour, "course": m.course}
            for m in sorted(loaded.meetings,
                            key=lambda m: (config.DAYS.index(m.day), m.hour))]
    return rows, loaded.tolerance_minutes, ""


@app.get("/api/setup")
def setup_state():
    """Everything the Setup page shows. Keys are masked; they never leave here."""
    values = setup_wizard.read_env(config.ENV_FILE)
    try:
        devices = [{"index": i, "name": n} for i, n in recording.list_devices()]
    except OSError:
        devices = []
    rows, tolerance, schedule_error = _schedule_rows()
    notion_on = bool(values.get("NOTION_TOKEN") and values.get("NOTION_DATABASE"))
    return jsonify({
        "home": str(config.HOME_DIR),
        "openai": setup_wizard.mask(values.get("OPENAI_API_KEY", "")),
        "openai_set": bool(values.get("OPENAI_API_KEY")),
        "anthropic": setup_wizard.mask(values.get("ANTHROPIC_API_KEY", "")),
        "anthropic_set": bool(values.get("ANTHROPIC_API_KEY")),
        "device": values.get("RECORD_DEVICE", "") or config.RECORD_DEVICE,
        "devices": devices,
        "schedule": rows,
        "tolerance": tolerance,
        "schedule_error": schedule_error,
        "days": list(config.DAYS),
        "notion": {
            "enabled": notion_on,
            "token": setup_wizard.mask(values.get("NOTION_TOKEN", "")),
            "token_set": bool(values.get("NOTION_TOKEN")),
            "database": values.get("NOTION_DATABASE", "") if notion_on else "",
        },
        "drive": {
            "connected": config.TOKEN_FILE.exists(),
            "running": _drive_login["running"],
            "error": _drive_login["error"],
            "account": _drive_account_state(),
        },
        "configured": _configured(),
    })


def _drive_account_state() -> dict:
    """The account's Drive grant, for the Setup page. Asks the service."""
    if not (account.enabled() and account.load() is not None):
        return {"available": False}
    out = {"available": True, "connect_url": account.url() + "/drive/connect",
           "connected": False, "google_email": "", "error": ""}
    try:
        status = account.drive_status()
        out["connected"] = bool(status.get("connected"))
        out["google_email"] = str(status.get("google_email") or "")
        out["revoked_reason"] = str(status.get("revoked_reason") or "")
    except Exception as exc:
        cached = account.drive_cached()
        out["connected"] = bool(cached.get("connected"))
        out["google_email"] = str(cached.get("google_email") or "")
        out["error"] = f"could not reach the account service: {exc}"
    return out


@app.post("/api/setup")
def setup_save():
    """Write .env and schedule.toml from the form, then reload settings.

    A blank key means "keep the one already on file", so the page never has
    to show a real key to let the user leave it alone.
    """
    payload = request.get_json(silent=True) or {}
    values = setup_wizard.read_env(config.ENV_FILE)

    for field, key in (("openai_key", "OPENAI_API_KEY"),
                       ("anthropic_key", "ANTHROPIC_API_KEY")):
        given = str(payload.get(field) or "").strip()
        if given:
            values[key] = given
    if not values.get("OPENAI_API_KEY") or not values.get("ANTHROPIC_API_KEY"):
        return jsonify({"ok": False, "error": "both API keys are needed"}), 400

    device = str(payload.get("device") or "").strip()
    values["RECORD_DEVICE"] = device or values.get("RECORD_DEVICE") or config.RECORD_DEVICE

    meetings = []
    for n, row in enumerate(payload.get("schedule") or [], start=1):
        try:
            meetings.append(config.Meeting(
                config.normalize_day(row.get("day", "")),
                config.normalize_hour(row.get("start", "")),
                config.normalize_course(row.get("course", "")),
            ))
        except (ValueError, AttributeError) as exc:
            return jsonify({"ok": False, "error": f"class row {n}: {exc}", "row": n}), 400
    if not meetings:
        return jsonify({"ok": False, "error": "add at least one class meeting"}), 400
    try:
        tolerance = int(payload.get("tolerance", config.DEFAULT_TOLERANCE_MINUTES))
        if tolerance < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "tolerance must be a whole number of minutes"}), 400

    notion = payload.get("notion") or {}
    skipped = not notion.get("enabled")
    if skipped:
        values["NOTION_TOKEN"] = ""
        values["NOTION_DATABASE"] = ""
    else:
        token = str(notion.get("token") or "").strip()
        database = str(notion.get("database") or "").strip()
        if token:
            values["NOTION_TOKEN"] = token
        if database:
            values["NOTION_DATABASE"] = database
        if not (values.get("NOTION_TOKEN") and values.get("NOTION_DATABASE")):
            return jsonify({"ok": False, "error": "Notion needs both the integration "
                            "secret and the database URL, or untick it to skip"}), 400

    config.ensure_home()
    setup_wizard.write_env(config.ENV_FILE, values, notion_skipped=skipped)
    config.write_schedule(meetings, tolerance)
    config.reload()
    # The account's copy follows the file, when this Mac is signed in.
    sync.sync_later("save")
    return jsonify({"ok": True, "configured": _configured(),
                    "classes": len(meetings)})


@app.get("/api/doctor")
def doctor_report():
    checks = doctor.run_checks()
    return jsonify({
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail,
                    "fix": c.fix, "required": c.required} for c in checks],
        # Not called "ok": the page's fetch helper reads ok=false as a failed
        # request, and a doctor report with a failing check is a good report.
        "healthy": all(c.ok or not c.required for c in checks),
    })


def _run_drive_login() -> None:
    from intake import upload
    try:
        upload.get_credentials(interactive=True)
        _drive_login["error"] = ""
    except Exception as exc:  # reported to the page, never raised into Flask
        _drive_login["error"] = str(exc)
    finally:
        _drive_login["running"] = False


@app.post("/api/drive/login")
def drive_login():
    """Connect Drive: through the account when this Mac is signed in to one
    (the page opens the account's connect page), else the Desktop OAuth flow
    on this Mac, which opens the user's browser on its own. {"local": true}
    asks for the latter regardless."""
    payload = request.get_json(silent=True) or {}
    if account.enabled() and account.load() is not None and not payload.get("local"):
        account.forget_drive_token()
        return jsonify({"ok": True, "open": account.url() + "/drive/connect"})
    if config.TOKEN_FILE.exists():
        return jsonify({"ok": False, "error": "Drive is already connected"}), 409
    if _drive_login["running"]:
        return jsonify({"ok": False, "error": "a login is already in progress"}), 409
    _drive_login.update(running=True, error="")
    threading.Thread(target=_run_drive_login, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/drive/disconnect")
def drive_disconnect():
    try:
        config.TOKEN_FILE.unlink(missing_ok=True)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


# --- The Syllabus account this Mac belongs to --------------------------------

@app.get("/api/account")
def account_state():
    """Everything the Setup page's account card shows.

    Asks the service to confirm the token when there is one, so a Mac that
    was removed from the account page shows as signed out here within one
    load rather than looking signed in forever.
    """
    out = account.summary()
    if not out["enabled"]:
        return jsonify(out)
    out["claim"] = account.claim_status()
    if out["signed_in"]:
        state, detail = account.whoami()
        out["check"] = {"state": state, "detail": detail}
        if state == "revoked":
            out = {**account.summary(), "claim": out["claim"],
                   "check": {"state": state, "detail": detail}}
        else:
            # Opening the Setup page is a good moment to pick up a schedule
            # another Mac saved; throttled so a page left open is quiet.
            sync.sync_later("setup", throttle=True)
            out["sync"] = sync.status()
            # This Mac's address on the web, and whether the socket is up.
            out["relay"] = relay.status()
    return jsonify(out)


@app.post("/api/account/claim")
def account_claim():
    payload = request.get_json(silent=True) or {}
    try:
        started = account.start_claim(str(payload.get("name") or ""))
    except RuntimeError as exc:
        code = 503 if "turned off" in str(exc) else 409
        return jsonify({"ok": False, "error": str(exc)}), code
    except Exception as exc:  # the service is unreachable
        return jsonify({"ok": False, "error": f"could not reach the account "
                        f"service: {exc}"}), 502
    started.pop("device_code", None)  # the poller's secret, not the page's
    return jsonify({"ok": True, **started})


@app.post("/api/account/cancel")
def account_cancel():
    account.cancel_claim()
    return jsonify({"ok": True})


@app.post("/api/account/signout")
def account_signout():
    account.sign_out()
    # The socket belonged to the device that just signed out.
    relay.reconnect()
    return jsonify({"ok": True})


def _is_loopback(host: str) -> bool:
    """Whether a --host value stays on this machine.

    The panel has no authentication of any kind, by design: it is a local
    control surface. So the only hosts it binds to unasked are loopback ones.
    """
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _open_browser_later(url: str, delay: float = 0.8) -> None:
    """Open the panel once the server has had a moment to bind."""
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake panel",
        description=f"Local control panel for {config.PROFILE.title}.")
    parser.add_argument("--port", type=int, default=config.PROFILE.panel_port,
                        help=f"{config.PROFILE.panel_port} for this profile")
    parser.add_argument("--host", default="127.0.0.1",
                        help="localhost by default; this can start processes")
    parser.add_argument("--expose", action="store_true",
                        help="allow --host to reach beyond this Mac. There is no "
                             "login on the panel: anyone who can reach the port "
                             "can start processes and rewrite your keys")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the page in a browser")
    args = parser.parse_args(argv)

    if not _is_loopback(args.host) and not args.expose:
        print(f"refusing to bind the panel to {args.host}: it has no login, and "
              f"anyone who can reach it can start and stop processes, read "
              f"pipeline.log, and rewrite the keys in .env. Keep it on this Mac, "
              f"or put something that authenticates in front of it (an SSH "
              f"tunnel, or a Cloudflare Tunnel with this Mac signed in to a Syllabus account) and pass --expose "
              f"to say you have.", file=sys.stderr, flush=True)
        return 2

    url = prepare(args.host, args.port)
    if not args.no_browser:
        _open_browser_later(url)
    serve(args.host, args.port)
    return 0


def start_url(host: str, port: int) -> str:
    """Where the panel opens: the dashboard, or Setup while nothing is configured."""
    page = "" if _configured() else "setup"
    return f"http://{host}:{port}/{page}"


def prepare(host: str, port: int) -> str:
    """Everything the panel does before it listens. Returns the page to open.

    Shared by `intake panel` and the desktop app (app.py), which runs the
    server from a thread so its window can have the main one.
    """
    url = start_url(host, port)
    print(f"{config.PROFILE.title} control panel:  {url}", file=sys.stderr, flush=True)
    if not _configured():
        print("  nothing is set up yet, so the Setup page opens first",
              file=sys.stderr, flush=True)
    # Say so now, in the terminal, if a lecture is already being recorded;
    # the page will show it too once it loads.
    _current_recorder()
    # A schedule saved on another Mac arrives now; the network never holds
    # the panel up, and a Mac with no account does nothing here.
    sync.sync_later("start")
    # The panel's place on the web: a socket to the account service, held
    # open from a thread, over which browsers at this Mac's address reach
    # it. Nothing without an account; it waits for one.
    relay.start(app)
    # Once a day, from a thread: is there a newer Syllabus to point at.
    updates.check_later()
    return url


def serve(host: str, port: int) -> None:
    """Listen until the process ends."""
    # load_dotenv=False: Flask would otherwise read a .env from the current
    # directory into the environment, so running the panel from a checkout
    # (or any folder with a stray .env) silently overrode the home directory's
    # settings. The only .env that counts is the one config already loaded.
    app.run(host=host, port=port, debug=False, threaded=True, load_dotenv=False)


if __name__ == "__main__":
    raise SystemExit(main())
