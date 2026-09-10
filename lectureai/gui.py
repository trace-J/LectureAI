"""A small local control panel for the lecture pipeline.

    lectureai panel          # opens http://127.0.0.1:5173 in your browser

Start and stop a recording, see whether the watcher is running, and check on
recent lectures, without remembering any commands. It drives the same modules
the CLI does, so anything started here behaves identically to the CLI.

The Setup page (/setup) is the browser version of `lectureai setup`: keys,
microphone, class schedule, Notion, and the Google Drive login, writing the
same .env and schedule.toml into the home directory. The panel opens it
first when nothing is configured yet.

Bound to localhost on purpose. It can start and stop processes and read your
pipeline log, none of which should be reachable from the network.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from lectureai import config, doctor, setup_wizard
from lectureai import notion_tasks
from lectureai import record as recording
from lectureai import transcribe

app = Flask(__name__)

# One recorder for the process. The GUI is single-user by construction, and a
# second concurrent recording would fight over the microphone anyway.
_recorder: recording.Recorder | None = None

WATCHER_LOG = config.WORK_DIR / "watcher-gui.log"


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


def _watcher_pid() -> int | None:
    """PID of the running watcher, from the lock file, or None."""
    if not config.LOCK_FILE.exists():
        return None
    try:
        pid = int(config.LOCK_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


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


def _recent(limit: int = 12) -> list[dict]:
    """Recent lectures, newest first, parsed out of pipeline.log.

    Success lines carry five tab-separated fields and error lines four, so the
    field count is what distinguishes them. A sixth field, when present, says
    what did not reach Notion; lines written before that field existed have
    five and are read exactly as before.
    """
    if not config.LOG_FILE.exists():
        return []
    rows = []
    for line in config.LOG_FILE.read_text().splitlines():
        fields = line.split("\t")
        if len(fields) in (5, 6):
            when, course, source, name, url = fields[:5]
            warning = fields[5] if len(fields) == 6 else ""
            rows.append({"when": when, "course": course, "source": source,
                         "name": name, "url": url, "error": None,
                         "warning": warning})
        elif len(fields) == 4 and fields[1] == "ERROR":
            when, _, source, message = fields
            rows.append({"when": when, "course": "ERROR", "source": source,
                         "name": "", "url": "", "error": message,
                         "warning": ""})
    return list(reversed(rows))[:limit]


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


def _integrations() -> dict:
    """Which pieces are configured. Presence only; no secrets leave here."""
    return {
        "openai": bool(config.OPENAI_API_KEY),
        "anthropic": bool(config.ANTHROPIC_API_KEY),
        "drive": config.TOKEN_FILE.exists(),
        "notion": notion_tasks.enabled(),
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/setup")
def setup_page():
    return render_template("setup.html")


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
        },
        "watcher": {"running": pid is not None, "pid": pid},
        "processing": _processing(pid),
        "inbox": _inbox(),
        "recent": _recent(),
        "integrations": _integrations(),
        "courses": _courses(),
        "now_class": _current_class(),
        "configured": _configured(),
    })


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
    # Run as a module from the directory that holds the package, so this works
    # from a checkout (where that is the repo root) and from a pipx install
    # (where it is site-packages) without either needing the other's setup.
    subprocess.Popen(
        [sys.executable, "-m", "lectureai.watch"],
        cwd=str(config.CODE_ROOT), stdin=subprocess.DEVNULL,
        stdout=handle, stderr=handle, start_new_session=True,
    )
    return jsonify({"ok": True})


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
        },
        "configured": _configured(),
    })


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
    from lectureai import upload
    try:
        upload.get_credentials(interactive=True)
        _drive_login["error"] = ""
    except Exception as exc:  # reported to the page, never raised into Flask
        _drive_login["error"] = str(exc)
    finally:
        _drive_login["running"] = False


@app.post("/api/drive/login")
def drive_login():
    """Start the Google OAuth flow. It opens the user's browser on its own."""
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


def _open_browser_later(url: str, delay: float = 0.8) -> None:
    """Open the panel once the server has had a moment to bind."""
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lectureai panel",
                                     description="Local control panel for LectureAI.")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--host", default="127.0.0.1",
                        help="localhost by default; this can start processes")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the page in a browser")
    args = parser.parse_args(argv)

    page = "" if _configured() else "setup"
    url = f"http://{args.host}:{args.port}/{page}"
    print(f"LectureAI control panel:  {url}", file=sys.stderr, flush=True)
    if not _configured():
        print("  nothing is set up yet, so the Setup page opens first",
              file=sys.stderr, flush=True)
    # Say so now, in the terminal, if a lecture is already being recorded;
    # the page will show it too once it loads.
    _current_recorder()
    if not args.no_browser:
        _open_browser_later(url)
    # load_dotenv=False: Flask would otherwise read a .env from the current
    # directory into the environment, so running the panel from a checkout
    # (or any folder with a stray .env) silently overrode the home directory's
    # settings. The only .env that counts is the one config already loaded.
    app.run(host=args.host, port=args.port, debug=False, threaded=True,
            load_dotenv=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
