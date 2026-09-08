"""Record a lecture from this Mac's microphone, straight into inbox/.

    python record.py                  # record until Ctrl-C
    python record.py --minutes 80     # or stop on its own
    python record.py --list-devices

Recording goes to .work/ and is moved into inbox/ only once it is finalized,
so the watcher never sees a half-written file. Everything downstream is
unchanged: the watcher picks it up from inbox/ exactly as it would a recording
synced from a phone.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
import transcribe

DEVICE_LINE = re.compile(r"^\[AVFoundation indev @ [^\]]*\] \[(\d+)\] (.+)$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def list_devices() -> list[tuple[int, str]]:
    """The Mac's audio input devices, as (index, name) in ffmpeg's numbering.

    ffmpeg reports the device list on stderr and then exits non-zero, because
    listing devices is not a valid transcode. That failure is expected.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )

    devices: list[tuple[int, str]] = []
    in_audio = False
    for line in proc.stderr.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if "AVFoundation video devices:" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        match = DEVICE_LINE.match(line)
        if match:
            devices.append((int(match.group(1)), match.group(2).strip()))
    return devices


def resolve_device(spec: str | None = None) -> tuple[int, str]:
    """Pick an input device, by index or by name substring.

    Resolving by name each time is the point: the index for a given microphone
    changes as other devices come and go, so a remembered index silently starts
    pointing at something else.
    """
    devices = list_devices()
    if not devices:
        raise RuntimeError(
            "ffmpeg found no audio input devices. If this Mac has a "
            "microphone, the terminal probably lacks permission to use it: "
            "System Settings > Privacy & Security > Microphone."
        )

    wanted = spec if spec is not None else config.RECORD_DEVICE
    candidates = [wanted, *config.RECORD_DEVICE_FALLBACKS] if spec is None else [wanted]

    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text.isdigit():
            index = int(text)
            for device_index, name in devices:
                if device_index == index:
                    return device_index, name
            raise RuntimeError(
                f"no audio device with index {index}. "
                f"Run:  python record.py --list-devices"
            )
        for device_index, name in devices:
            if text.lower() in name.lower():
                return device_index, name

    if spec is not None:
        raise RuntimeError(
            f"no audio device matching {spec!r}. "
            f"Run:  python record.py --list-devices"
        )

    fallback = devices[0]
    log(f"  no preferred microphone attached; using {fallback[1]}")
    return fallback


def output_name(started: datetime, course: str | None = None) -> str:
    """Filename for a recording that began at `started`.

    The course code goes in the name so the pipeline's filename fallback can
    still place the lecture if the schedule lookup misses at processing time.
    """
    resolved = course or config.infer_course(started)
    stamp = started.strftime("%Y-%m-%d_%H%M")
    if resolved == config.UNKNOWN_COURSE:
        return f"lecture_{stamp}.m4a"
    return f"{resolved}_{stamp}.m4a"


def _ffmpeg_command(device_index: int, destination: Path) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{device_index}",
        "-ac", str(config.RECORD_CHANNELS),
        "-ar", str(config.RECORD_SAMPLE_RATE),
        "-c:a", "aac", "-b:a", config.RECORD_BITRATE,
        "-y", str(destination),
    ]


def _stop(proc: subprocess.Popen) -> None:
    """Ask ffmpeg to finalize the file, escalating only if it won't.

    An m4a needs its trailer written when recording stops. Killing ffmpeg
    outright leaves a file no player and no transcription API will open, so
    the lecture would be gone. SIGINT is what tells it to close cleanly.
    """
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=20)
        return
    except subprocess.TimeoutExpired:
        log("  ffmpeg did not stop on its own; terminating")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log("  ffmpeg still running; killing it (the file may be unusable)")
        proc.kill()
        proc.wait()


def _watcher_is_running() -> bool:
    """Whether a watch.py process currently holds the inbox lock."""
    if not config.LOCK_FILE.exists():
        return False
    try:
        pid = int(config.LOCK_FILE.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def record(
    minutes: float | None = None,
    device: str | None = None,
    course: str | None = None,
) -> Path:
    """Record from the microphone and move the result into inbox/.

    Returns the path of the finished recording.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not on your PATH. Install it:  brew install ffmpeg")

    device_index, device_name = resolve_device(device)
    limit = minutes if minutes is not None else config.RECORD_MAX_MINUTES
    started = datetime.now()

    config.WORK_DIR.mkdir(exist_ok=True)
    staging = config.WORK_DIR / f"recording_{started:%Y%m%d-%H%M%S}.m4a"

    planned = output_name(started, course)
    log(f"recording from {device_name}")
    log(f"  will file as {planned}")
    log("  the mic takes a few seconds to spin up, so start before the lecture does")
    log(f"  Ctrl-C to stop" + (f", or it stops itself at {limit:g} min" if limit else ""))

    errors = tempfile.TemporaryFile(mode="w+")
    proc = subprocess.Popen(
        _ffmpeg_command(device_index, staging),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
        # Keep ffmpeg out of the terminal's signal group so Ctrl-C reaches this
        # process first and we can shut it down in an orderly way.
        start_new_session=True,
    )

    deadline = time.monotonic() + limit * 60 if limit else None
    interrupted = False
    try:
        while proc.poll() is None:
            elapsed = str(datetime.now() - started).split(".")[0]
            size = staging.stat().st_size if staging.exists() else 0
            # ffmpeg buffers the m4a, so the file sits at zero for the first
            # while. Showing 0.0MB during a lecture reads like a failure, so
            # only report a size once there is one.
            shown = f"   {size / 1024 / 1024:.1f}MB" if size else ""
            print(f"\r  recording  {elapsed}{shown}   ",
                  end="", file=sys.stderr, flush=True)
            if deadline and time.monotonic() >= deadline:
                log("\n  reached the time limit; stopping")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        print("", file=sys.stderr, flush=True)
        _stop(proc)

    errors.seek(0)
    stderr = errors.read().strip()
    errors.close()

    if proc.returncode not in (0, 255) and not interrupted and not staging.exists():
        raise RuntimeError(_diagnose(stderr, device_name))

    if not staging.exists() or staging.stat().st_size == 0:
        staging.unlink(missing_ok=True)
        raise RuntimeError(_diagnose(stderr, device_name))

    # ffmpeg's last write is when recording stopped, which is exactly what the
    # course-inference in config.py expects to read off the file. A rename
    # inside the project keeps it, so don't copy the file around.
    destination = config.INBOX_DIR / output_name(started, course)
    if destination.exists():
        destination = config.INBOX_DIR / output_name(started, course).replace(
            ".m4a", f"_{int(time.time())}.m4a"
        )
    shutil.move(str(staging), str(destination))

    size_mb = destination.stat().st_size / 1024 / 1024

    # Report what the file actually holds, not how long we sat here. macOS
    # takes a second or more to open the capture device, and everything
    # downstream measures the lecture from this duration, so the wall clock
    # would overstate it.
    captured = transcribe.duration_seconds(destination) or 0
    clock = str(timedelta(seconds=int(captured)))
    log(f"  saved {destination.name} ({size_mb:.1f}MB, {clock} of audio)")

    lost = (datetime.now() - started).total_seconds() - captured
    if captured and lost > 3:
        log(f"  note: {lost:.0f}s at the start was lost while the mic opened")
    if stderr:
        log(f"  ffmpeg said: {stderr.splitlines()[-1]}")

    if _watcher_is_running():
        log("  the watcher is running and will pick it up")
    else:
        log(f"  no watcher running. Process it with:")
        log(f"    python watch.py --once inbox/{destination.name}")

    return destination


def _diagnose(stderr: str, device_name: str) -> str:
    """Turn an ffmpeg failure into something worth reading."""
    lowered = stderr.lower()
    if "permission" in lowered or "not authorized" in lowered or not stderr:
        return (
            f"could not record from {device_name}. The most likely cause is "
            f"microphone permission: open System Settings > Privacy & Security "
            f"> Microphone and enable it for your terminal, then try again."
            + (f"\nffmpeg said: {stderr}" if stderr else "")
        )
    return f"recording failed.\nffmpeg said: {stderr}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record a lecture from this Mac's microphone into inbox/."
    )
    parser.add_argument("--minutes", type=float, default=None,
                        help="stop automatically after this long")
    parser.add_argument("--device", default=None,
                        help="microphone name substring or avfoundation index")
    parser.add_argument("--course", default=None,
                        help="course code to file under, overriding the schedule")
    parser.add_argument("--list-devices", action="store_true",
                        help="show the audio inputs ffmpeg can see, and exit")
    args = parser.parse_args()

    if args.list_devices:
        devices = list_devices()
        if not devices:
            log("no audio input devices found")
            return 1
        try:
            active_index, _ = resolve_device(None)
        except RuntimeError:
            active_index = None
        for index, name in devices:
            marker = " <- default" if index == active_index else ""
            print(f"  [{index}] {name}{marker}")
        return 0

    course = args.course
    if course:
        known = {code.upper(): code for code in config.SCHEDULE.values()}
        if course.upper() not in known:
            log(f"error: {course} is not in SCHEDULE. Known: "
                f"{', '.join(sorted(known.values()))}")
            return 1
        course = known[course.upper()]

    try:
        record(minutes=args.minutes, device=args.device, course=course)
    except RuntimeError as exc:
        log(f"error: {exc}")
        return 1
    except Exception as exc:
        log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
