"""A small local control panel for the lecture pipeline.

    python gui.py            # then open http://127.0.0.1:5173

Start and stop a recording, see whether the watcher is running, and check on
recent lectures, without remembering any commands. It drives the same modules
the CLI does, so anything started here behaves identically to the CLI.

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
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import config
import notion_tasks
import record as recording
import transcribe

app = Flask(__name__)

# One recorder for the process. The GUI is single-user by construction, and a
# second concurrent recording would fight over the microphone anyway.
_recorder: recording.Recorder | None = None

WATCHER_LOG = config.BASE_DIR / ".work" / "watcher-gui.log"


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


def _current_class() -> str | None:
    """The course scheduled right now, so the button can name it."""
    course = config.infer_course(datetime.now())
    return None if course == config.UNKNOWN_COURSE else course


def _inbox() -> list[dict]:
    """Recordings waiting to be processed."""
    items = []
    for path in sorted(config.INBOX_DIR.iterdir()):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and not path.name.startswith("."):
            items.append({"name": path.name, "bytes": path.stat().st_size})
    return items


def _recent(limit: int = 12) -> list[dict]:
    """Recent lectures, newest first, parsed out of pipeline.log.

    Success lines carry five tab-separated fields and error lines four, so the
    field count is what distinguishes them.
    """
    if not config.LOG_FILE.exists():
        return []
    rows = []
    for line in config.LOG_FILE.read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 5:
            when, course, source, name, url = fields
            rows.append({"when": when, "course": course, "source": source,
                         "name": name, "url": url, "error": None})
        elif len(fields) == 4 and fields[1] == "ERROR":
            when, _, source, message = fields
            rows.append({"when": when, "course": "ERROR", "source": source,
                         "name": "", "url": "", "error": message})
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


@app.get("/api/status")
def status():
    pid = _watcher_pid()
    active = _recorder is not None and _recorder.is_recording
    return jsonify({
        "recording": {
            "active": active,
            "elapsed": round(_recorder.elapsed, 1) if active else 0,
            "device": _recorder.device_name if active else "",
            "planned": _recorder.planned_name if active else "",
            "bytes": _recorder.staged_bytes if active else 0,
            "course": (_recorder.course or "") if active else "",
        },
        "watcher": {"running": pid is not None, "pid": pid},
        "processing": _processing(pid),
        "inbox": _inbox(),
        "recent": _recent(),
        "integrations": _integrations(),
        "courses": sorted(set(config.SCHEDULE.values())),
        "now_class": _current_class(),
    })


@app.post("/api/record/start")
def record_start():
    global _recorder
    if _recorder is not None and _recorder.is_recording:
        return jsonify({"ok": False, "error": "already recording"}), 409

    payload = request.get_json(silent=True) or {}
    course = (payload.get("course") or "").strip() or None
    if course and course not in set(config.SCHEDULE.values()):
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
    if _recorder is None:
        return jsonify({"ok": False, "error": "not recording"}), 409
    try:
        destination = _recorder.stop()
    except Exception as exc:
        _recorder = None
        return jsonify({"ok": False, "error": str(exc)}), 500
    wall = _recorder.elapsed
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
    WATCHER_LOG.parent.mkdir(exist_ok=True)
    handle = WATCHER_LOG.open("a")
    # start_new_session so the watcher outlives this GUI: closing the control
    # panel should not abandon a lecture that is midway through transcription.
    subprocess.Popen(
        [sys.executable, str(config.BASE_DIR / "watch.py")],
        cwd=str(config.BASE_DIR), stdin=subprocess.DEVNULL,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Local control panel for LectureAI.")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--host", default="127.0.0.1",
                        help="localhost by default; this can start processes")
    args = parser.parse_args()

    print(f"LectureAI control panel:  http://{args.host}:{args.port}",
          file=sys.stderr, flush=True)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
