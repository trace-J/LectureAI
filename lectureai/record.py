"""Record a lecture from this Mac's microphone, straight into inbox/.

    lectureai record                  # record until Ctrl-C
    lectureai record --minutes 80     # or stop on its own
    lectureai record --list-devices

Recording goes to .work/ and is moved into inbox/ only once it is finalized,
so the watcher never sees a half-written file. Everything downstream is
unchanged: the watcher picks it up from inbox/ exactly as it would a recording
synced from a phone.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from lectureai import config
from lectureai import transcribe

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
                f"Run:  lectureai record --list-devices"
            )
        for device_index, name in devices:
            if text.lower() in name.lower():
                return device_index, name

    if spec is not None:
        raise RuntimeError(
            f"no audio device matching {spec!r}. "
            f"Run:  lectureai record --list-devices"
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


def _ffmpeg_command(device_index: int, destination: Path,
                    max_minutes: float | None = None) -> list[str]:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{device_index}",
        "-ac", str(config.RECORD_CHANNELS),
        "-ar", str(config.RECORD_SAMPLE_RATE),
        "-c:a", "aac", "-b:a", config.RECORD_BITRATE,
    ]
    # ffmpeg enforces the ceiling itself and finalizes the file when it gets
    # there. Whoever started it may be long gone by then (the panel that
    # launched it was closed, the terminal died), and a recording with no
    # supervisor must still end in a playable file rather than run until the
    # disk is full.
    if max_minutes:
        command += ["-t", f"{max_minutes * 60:g}"]
    return command + ["-y", str(destination)]


class _ExternalProcess:
    """A Popen-shaped handle on a process this Python did not start.

    Only what _stop and Recorder need: poll, send_signal, terminate, kill, and
    wait. The exit status of a process that isn't our child can't be read, so
    poll reports 0 once it is gone.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        # If it happens to be our own child (the tests do this), reap it, or
        # its zombie keeps answering os.kill(pid, 0) forever.
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            if pid != 0:
                self.returncode = os.waitstatus_to_exitcode(status)
                return self.returncode
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self.returncode = 0
        except PermissionError:
            return None
        return self.returncode

    def send_signal(self, sig: int) -> None:
        if self.poll() is None:
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                self.returncode = 0

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(f"pid {self.pid}", timeout)
            time.sleep(0.2)
        return self.returncode


def _process_is_recording(pid: int, staging: Path) -> bool:
    """Whether `pid` is alive and is the ffmpeg writing `staging`.

    Pids get reused, so a live pid from the state file is not proof on its
    own; the command line has to name our output file too.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    proc = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True)
    command = proc.stdout.strip()
    return "ffmpeg" in command and staging.name in command


def _write_state(pid: int, staging: Path, started: datetime,
                 course: str | None, device_name: str) -> None:
    config.RECORDING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": pid, "staging": str(staging), "started": started.isoformat(),
               "course": course, "device": device_name}
    tmp = config.RECORDING_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(config.RECORDING_STATE_FILE)


def _read_state() -> dict | None:
    try:
        data = json.loads(config.RECORDING_STATE_FILE.read_text())
        return {
            "pid": int(data["pid"]),
            "staging": Path(data["staging"]),
            "started": datetime.fromisoformat(data["started"]),
            "course": data.get("course") or None,
            "device": data.get("device") or "",
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _clear_state() -> None:
    config.RECORDING_STATE_FILE.unlink(missing_ok=True)


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
    """Whether a watcher process currently holds the inbox lock."""
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


class Recorder:
    """One microphone recording, startable and stoppable from anywhere.

    The CLI runs a progress loop around this; the GUI holds one and drives it
    from button clicks. Keeping the ffmpeg handling, the finalize, and the
    move into inbox/ in one place means the two front ends cannot drift into
    producing subtly different files.
    """

    def __init__(self, device: str | None = None, course: str | None = None):
        self.device = device
        self.course = course
        self.device_name = ""
        self.started: datetime | None = None
        # True when this Recorder picked up an ffmpeg that an earlier panel or
        # CLI started and then lost, rather than starting one itself.
        self.adopted = False
        self._proc: subprocess.Popen | _ExternalProcess | None = None
        self._staging: Path | None = None
        self._errors: Path | None = None

    @property
    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def elapsed(self) -> float:
        """Seconds since recording began. Wall clock, not audio captured."""
        if not self.started:
            return 0.0
        return (datetime.now() - self.started).total_seconds()

    @property
    def staged_bytes(self) -> int:
        if self._staging and self._staging.exists():
            return self._staging.stat().st_size
        return 0

    @property
    def planned_name(self) -> str:
        return output_name(self.started or datetime.now(), self.course)

    def start(self, max_minutes: float | None = None) -> None:
        """Open the microphone and begin writing to .work/.

        `max_minutes` caps the recording inside ffmpeg itself; None means
        config.RECORD_MAX_MINUTES.
        """
        if self.is_recording:
            raise RuntimeError("already recording")
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "ffmpeg is not on your PATH. Install it:  brew install ffmpeg")
        other = Recorder.adopt()
        if other is not None:
            raise RuntimeError(
                f"a recording is already running: {other.planned_name}, started "
                f"{other.started:%H:%M} by an earlier panel or terminal. Stop "
                f"that one first; the panel shows it, or run  lectureai record"
            )

        device_index, self.device_name = resolve_device(self.device)
        self.started = datetime.now()
        config.WORK_DIR.mkdir(exist_ok=True)
        self._staging = config.WORK_DIR / f"recording_{self.started:%Y%m%d-%H%M%S}.m4a"

        # ffmpeg's complaints go to a file next to the recording, not an
        # anonymous temp file, so a later process that adopts this recording
        # can still read why it went wrong.
        self._errors = self._staging.with_suffix(".log")
        limit = max_minutes if max_minutes is not None else config.RECORD_MAX_MINUTES
        with self._errors.open("w") as errors:
            self._proc = subprocess.Popen(
                _ffmpeg_command(device_index, self._staging, limit),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=errors,
                # Keep ffmpeg out of the terminal's signal group so Ctrl-C
                # reaches this process first and we can shut it down in an
                # orderly way. It also means ffmpeg outlives us if we are
                # killed, which is what the state file is for.
                start_new_session=True,
            )
        _write_state(self._proc.pid, self._staging, self.started,
                     self.course, self.device_name)

    @classmethod
    def adopt(cls) -> "Recorder | None":
        """Take over a recording that an earlier process started and lost.

        The panel gets closed, the terminal that ran `lectureai record` dies,
        and ffmpeg keeps recording on its own in its own session. Without this,
        the next panel opens saying "Not recording" while the microphone is
        still live, and the lecture ends up in .work/ with no trailer and no
        way to play it. Returns None when nothing is running.
        """
        state = _read_state()
        if state is None:
            return None
        if not _process_is_recording(state["pid"], state["staging"]):
            return None
        recorder = cls(course=state["course"])
        recorder.device_name = state["device"]
        recorder.started = state["started"]
        recorder.adopted = True
        recorder._staging = state["staging"]
        recorder._errors = state["staging"].with_suffix(".log")
        recorder._proc = _ExternalProcess(state["pid"])
        return recorder

    def finish(self) -> Path | None:
        """File a recording whose ffmpeg has already exited.

        Two front ends can hold the same recording (a panel that started it
        and a `lectureai record` that adopted it); whichever stops it first
        moves the file. The other finds the file gone and the state cleared,
        and must not mistake that for a failed recording. Returns None in
        that case, the inbox path otherwise.
        """
        if self.is_recording:
            raise RuntimeError("still recording; use stop()")
        if (self._staging is not None and not self._staging.exists()
                and _read_state() is None):
            self._proc = self._staging = self._errors = None
            return None
        return self.stop()

    def stop(self) -> Path:
        """Finalize the recording and move it into inbox/. Returns its path."""
        if self._proc is None or self._staging is None or self.started is None:
            raise RuntimeError("not recording")

        returncode_before = self._proc.poll()
        _stop(self._proc)

        stderr = ""
        if self._errors is not None:
            try:
                stderr = self._errors.read_text().strip()
            except OSError:
                pass
            self._errors.unlink(missing_ok=True)
        self._errors = None
        staging, self._staging = self._staging, None
        proc, self._proc = self._proc, None
        _clear_state()

        # ffmpeg exiting on its own before we asked means it never opened the
        # device, which is nearly always a permissions problem.
        died_early = returncode_before is not None and not staging.exists()
        if died_early or not staging.exists() or staging.stat().st_size == 0:
            staging.unlink(missing_ok=True)
            raise RuntimeError(_diagnose(stderr, self.device_name))

        destination = _file_into_inbox(staging, self.started, self.course)
        self.stderr = stderr
        return destination


def _file_into_inbox(staging: Path, started: datetime, course: str | None) -> Path:
    """Move a finalized recording into inbox/ under its lecture name."""
    destination = config.INBOX_DIR / output_name(started, course)
    if destination.exists():
        destination = config.INBOX_DIR / output_name(
            started, course
        ).replace(".m4a", f"_{int(time.time())}.m4a")
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(destination))
    return destination


def finish_abandoned() -> Path | None:
    """File a recording whose ffmpeg already exited with nobody to move it.

    ffmpeg stops on its own at the time cap and writes the trailer, but the
    move into inbox/ was the job of whoever started it. If that process is
    gone, the finished lecture sits in .work/ forever. Returns the inbox path
    when a file was moved, None otherwise. A file ffmpeg never finalized (it
    was killed outright) can't be salvaged; it is left in place and the state
    file is cleared so the panel stops asking.
    """
    state = _read_state()
    if state is None:
        return None
    if _process_is_recording(state["pid"], state["staging"]):
        return None
    _clear_state()
    staging = state["staging"]
    staging.with_suffix(".log").unlink(missing_ok=True)
    if not staging.exists() or staging.stat().st_size == 0:
        staging.unlink(missing_ok=True)
        return None
    if not transcribe.duration_seconds(staging):
        log(f"  {staging.name} was never finalized and cannot be played; "
            f"leaving it in {staging.parent}")
        return None
    return _file_into_inbox(staging, state["started"], state["course"])


def record(
    minutes: float | None = None,
    device: str | None = None,
    course: str | None = None,
) -> Path:
    """Record from the microphone and move the result into inbox/.

    Returns the path of the finished recording.
    """
    limit = minutes if minutes is not None else config.RECORD_MAX_MINUTES

    filed = finish_abandoned()
    if filed is not None:
        log(f"filed an earlier recording nobody stopped: {filed.name}")

    recorder = Recorder.adopt()
    if recorder is not None:
        log(f"picking up a recording already running since "
            f"{recorder.started:%H:%M} (pid {recorder._proc.pid})")
        log(f"  will file as {recorder.planned_name}")
        log("  Ctrl-C to stop")
        limit = None
    else:
        recorder = Recorder(device=device, course=course)
        recorder.start(max_minutes=limit)
        log(f"recording from {recorder.device_name}")
        log(f"  will file as {recorder.planned_name}")
        log("  the mic takes a few seconds to spin up, so start before the lecture does")
        log(f"  Ctrl-C to stop" + (f", or it stops itself at {limit:g} min" if limit else ""))
    started = recorder.started

    deadline = time.monotonic() + limit * 60 if limit else None
    try:
        while recorder.is_recording:
            elapsed = str(datetime.now() - started).split(".")[0]
            size = recorder.staged_bytes
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
        pass
    finally:
        print("", file=sys.stderr, flush=True)

    destination = recorder.stop()
    stderr = getattr(recorder, "stderr", "")

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
        log(f"    lectureai watch --once {destination}")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lectureai record",
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
    args = parser.parse_args(argv)

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

    try:
        known = {code.upper(): code for code in config.courses()}
    except config.ScheduleError as exc:
        log(f"error: {exc}")
        return 1

    course = args.course
    if course:
        if course.upper() not in known:
            log(f"error: {course} is not in your schedule. Known: "
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
