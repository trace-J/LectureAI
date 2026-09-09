"""Orchestrator: audio file in, transcript and summary out, filed in Drive.

    lectureai watch --once ~/.lectureai/inbox/lecture.m4a   # one file, then exit
    lectureai watch                                          # watch inbox/ until Ctrl-C

The watcher never dies on a bad file. Anything that fails is logged and left
in inbox/ so it can be retried, and the next recording still gets processed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from lectureai import config
from lectureai import notion_tasks
from lectureai import summarize
from lectureai import transcribe
from lectureai import upload as drive


def acquire_single_instance_lock():
    """Fail fast if another watcher is already running.

    Two watchers on the same inbox both transcribe every file — paying twice —
    and race on the same staging paths, so whichever finishes first deletes the
    other's files mid-upload. The flock is released automatically when the
    process exits, including on a crash or SIGKILL.
    """
    handle = config.LOCK_FILE.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(
            f"another watcher already holds {config.LOCK_FILE.name}. "
            f"Stop it first:  pkill -f 'lectureai.*watch'"
        )
    handle.write(str(os.getpid()))
    handle.flush()
    return handle  # keep a reference alive; closing it drops the lock


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def write_log_line(*fields: str) -> None:
    """Append one tab-separated record to pipeline.log."""
    stamp = datetime.now().isoformat(timespec="seconds")
    with config.LOG_FILE.open("a") as fh:
        fh.write("\t".join([stamp, *fields]) + "\n")


def write_status(stage: str, file: str = "", course: str = "",
                 detail: str = "", started: str = "") -> None:
    """Record what the watcher is doing, for the control panel to read.

    pipeline.log gets its line only once a lecture is finished, so without
    this an 80 minute recording spends a quarter of an hour looking like it
    is sitting untouched in the inbox.

    Written to a temp file and renamed, because the panel polls this once a
    second and must never catch a half-written file. Failures are swallowed:
    progress reporting must not be able to take down a lecture.
    """
    try:
        config.STATUS_FILE.parent.mkdir(exist_ok=True)
        payload = {
            "stage": stage, "file": file, "course": course, "detail": detail,
            "started": started or datetime.now().isoformat(timespec="seconds"),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
        }
        temp = config.STATUS_FILE.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload))
        temp.replace(config.STATUS_FILE)
    except OSError:
        pass


def clear_status() -> None:
    """Say the watcher is idle again."""
    try:
        config.STATUS_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def is_audio(path: Path) -> bool:
    return (
        path.suffix.lower() in config.AUDIO_EXTENSIONS
        and not path.name.startswith(".")
    )


def wait_until_stable(path: Path) -> None:
    """Block until the file size holds steady, so we don't read a partial sync."""
    last_size = -1
    steady_since = None
    deadline = time.monotonic() + config.STABILITY_TIMEOUT_SECONDS

    while True:
        if not path.exists():
            raise FileNotFoundError(f"{path.name} disappeared while waiting")
        size = path.stat().st_size

        if size == last_size and size > 0:
            if steady_since is None:
                steady_since = time.monotonic()
            if time.monotonic() - steady_since >= config.STABILITY_SECONDS:
                return
        else:
            if last_size >= 0:
                log(f"  {path.name} still growing ({size / 1024 / 1024:.1f}MB) ...")
            steady_since = None
            last_size = size

        if time.monotonic() > deadline:
            raise TimeoutError(
                f"{path.name} never stopped changing after "
                f"{config.STABILITY_TIMEOUT_SECONDS}s"
            )
        time.sleep(config.STABILITY_POLL_SECONDS)


def preflight(interactive: bool) -> None:
    """Fail before spending money on transcription if something obvious is off."""
    config.schedule()
    config.require("OPENAI_API_KEY")
    config.require("ANTHROPIC_API_KEY")
    drive.get_service(interactive)


def process(audio_path: str | Path, interactive: bool = True) -> dict:
    """Run the full chain on one recording. Returns a summary of what happened."""
    path = Path(audio_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"no such audio file: {path}")

    mtime = path.stat().st_mtime
    length = transcribe.duration_seconds(path)
    course, source = config.resolve_course(path, mtime, length)
    date = config.lecture_date(mtime, length)
    log(f"processing {path.name} -> {course} on {date} (matched by {source})")
    if source == "filename":
        log("  timestamp matched no class; took the course from the filename")
    elif course == config.UNKNOWN_COURSE:
        log("  neither the timestamp nor the filename identifies a course; "
            "filing under UNKNOWN")

    began = datetime.now().isoformat(timespec="seconds")
    status = lambda stage, detail="": write_status(  # noqa: E731
        stage, path.name, course, detail, began)

    status("transcribing", "starting")
    transcript = transcribe.transcribe(
        path, on_progress=lambda detail: status("transcribing", detail))

    status("summarizing", f"{len(transcript.split()):,} words")
    result = summarize.summarize(transcript, course, date)
    stem = summarize.build_filename(course, date, result["topic_slug"])

    # Stage local copies first. If an upload fails, the expensive work survives
    # and the retry costs nothing but the upload.
    txt_path = config.PROCESSED_DIR / f"{stem}.txt"
    md_path = config.PROCESSED_DIR / f"{stem}.md"
    txt_path.write_text(transcript)
    md_path.write_text(summarize.render_markdown(result, course, date))

    # Identifies this recording on the Drive files it produces, so re-running
    # it replaces its own past output instead of colliding with a different
    # lecture that happens to share a name.
    rec_key = config.recording_key(mtime, length)
    time_suffix = config.recording_time_suffix(mtime, length)

    # The summary goes in the course folder as a Doc; the raw transcript goes
    # one level down, so the course folder stays a clean list of study notes.
    status("uploading", stem)
    summary = drive.upload(
        md_path, course, interactive,
        as_google_doc=config.SUMMARY_AS_GOOGLE_DOC,
        name=stem if config.SUMMARY_AS_GOOGLE_DOC else md_path.name,
        recording_key=rec_key, time_suffix=time_suffix,
    )
    md_url = summary.url

    # Name the transcript after whatever the summary ended up called, so a
    # renamed pair stays a pair.
    final_stem = summary.name.removesuffix(".md")
    transcript_upload = drive.upload(
        txt_path, course, interactive, subfolder=config.TRANSCRIPT_SUBFOLDER,
        name=f"{final_stem}.txt",
        recording_key=rec_key, time_suffix=time_suffix,
    )
    txt_url = transcript_upload.url

    # Both uploads landed, so the local staging copies are redundant.
    txt_path.unlink(missing_ok=True)
    md_path.unlink(missing_ok=True)

    # Notion comes last and never raises. The lecture is already safe in Drive
    # by this point, so a Notion outage or a misconfigured database must not
    # cost the recording, and the tasks are recoverable from the summary Doc.
    notion_result = None
    if notion_tasks.enabled():
        status("notion", f"{len(result['action_items'])} action items")
        try:
            notion_result = notion_tasks.push(
                result["action_items"], course, source_url=md_url
            )
            log(f"  notion: {notion_result['added']} added, "
                f"{notion_result['skipped']} already there, "
                f"{notion_result['failed']} failed")
        except Exception as exc:
            log(f"  notion: skipped ({exc})")
    elif result["action_items"]:
        log(f"  {len(result['action_items'])} action items (Notion not configured)")

    if config.DELETE_ORIGINAL_AFTER_UPLOAD:
        path.unlink()
        log(f"  deleted original {path.name}")
        original = "(deleted)"
    else:
        destination = config.PROCESSED_DIR / path.name
        if destination.resolve() != path:
            shutil.move(str(path), str(destination))
        original = str(destination)

    write_log_line(course, path.name, final_stem, md_url)
    log(f"  done: {final_stem}")
    log(f"  summary:    {md_url}")
    log(f"  transcript: {txt_url}")

    return {
        "course": course, "date": date, "stem": final_stem,
        "summary_url": md_url, "transcript_url": txt_url,
        "original": original,
    }


class InboxHandler(FileSystemEventHandler):
    """Queue new audio files for the main thread to process one at a time."""

    def __init__(self, work_queue: queue.Queue):
        self.queue = work_queue

    def _enqueue(self, raw_path: str) -> None:
        path = Path(raw_path)
        if is_audio(path):
            log(f"noticed {path.name}")
            self.queue.put(path)

    def on_created(self, event):
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event):
        # Some sync clients write a temp file and rename it into place.
        if not event.is_directory:
            self._enqueue(event.dest_path)


def run_watcher() -> int:
    lock = acquire_single_instance_lock()  # noqa: F841 — held for process life
    preflight(interactive=False)

    work: queue.Queue = queue.Queue()
    seen: set[Path] = set()

    for existing in sorted(config.INBOX_DIR.iterdir()):
        if is_audio(existing):
            log(f"found waiting: {existing.name}")
            work.put(existing)

    observer = Observer()
    observer.schedule(InboxHandler(work), str(config.INBOX_DIR), recursive=False)
    observer.start()
    log(f"watching {config.INBOX_DIR} for "
        f"{', '.join(sorted(config.AUDIO_EXTENSIONS))} — Ctrl-C to stop")

    try:
        while True:
            try:
                path = work.get(timeout=1)
            except queue.Empty:
                continue

            resolved = path.resolve()
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)

            try:
                write_status("waiting", path.name, detail="waiting for the file to finish copying")
                wait_until_stable(path)
                process(path, interactive=False)
            except Exception as exc:
                # One bad file must never take the watcher down.
                log(f"ERROR on {path.name}: {exc}")
                traceback.print_exc(file=sys.stderr)
                write_log_line("ERROR", path.name, str(exc))
                if path.exists():
                    log(f"  left {path.name} in inbox for a retry")
                seen.discard(resolved)
            finally:
                # Idle again either way, so the panel stops showing a stage
                # that finished or died.
                clear_status()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        observer.stop()
        observer.join()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lectureai watch",
        description="Process lecture recordings into transcripts and summaries."
    )
    parser.add_argument("--once", metavar="FILE",
                        help="process a single recording and exit")
    args = parser.parse_args(argv)

    if args.once:
        try:
            lock = acquire_single_instance_lock()  # noqa: F841
            preflight(interactive=True)
            process(args.once, interactive=True)
        except Exception as exc:
            log(f"error: {exc}")
            traceback.print_exc(file=sys.stderr)
            return 1
        finally:
            clear_status()
        return 0

    try:
        return run_watcher()
    except RuntimeError as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
