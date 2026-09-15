"""Audio file -> transcript text, via whichever transcription provider is set.

Handles the two ways a lecture recording breaks a provider's request limit:
compress first, then split if compression wasn't enough.

Every limit that decides any of that comes from the provider (see
providers.py), never from a constant in here: the 25MB cap, the duration a
chunk may run to, and whether the model can silently truncate at all are
properties of the model, and they change when it does.

Progress goes to stderr, the transcript goes to stdout, so this works:
    python transcribe.py lecture.m4a > transcript.txt
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from intake import config, providers, tools
from intake.providers import TranscriptionProvider


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _size(path: Path) -> int:
    return path.stat().st_size


def _mb(n_bytes: int) -> str:
    return f"{n_bytes / 1024 / 1024:.1f}MB"


def _ffmpeg(args: list[str], what: str) -> None:
    """Run ffmpeg quietly; raise with its stderr if it fails."""
    result = subprocess.run(
        [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-5:]
        raise RuntimeError(f"ffmpeg failed while {what}:\n" + "\n".join(tail))


def duration_seconds(path: Path) -> float | None:
    """Length of the audio, or None if ffprobe can't tell."""
    result = subprocess.run(
        [
            tools.ffprobe(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def audio_codec(path: Path) -> str:
    """Codec name of the first audio stream, or "" if ffprobe can't tell."""
    result = subprocess.run(
        [
            tools.ffprobe(), "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def compress(src: Path, work_dir: Path) -> Path:
    """Re-encode to mono 64kbps AAC, which is plenty for speech."""
    dest = work_dir / f"{src.stem}_compressed.m4a"
    log(f"  compressing {_mb(_size(src))} -> mono 64kbps m4a ...")
    _ffmpeg(
        ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "aac", "-b:a", "64k", str(dest)],
        f"compressing {src.name}",
    )
    log(f"  compressed to {_mb(_size(dest))}")
    return dest


def split(src: Path, work_dir: Path, seconds: int) -> list[Path]:
    """Cut the audio into ~`seconds` pieces, in order."""
    # Its own directory with a fixed chunk name, so a stem that happens to
    # prefix another file's chunks can't pull them into this glob.
    chunk_dir = Path(tempfile.mkdtemp(prefix="chunks_", dir=work_dir))
    pattern = str(chunk_dir / "chunk_%03d.m4a")

    # Stream-copy when the source is already AAC (fast, lossless). Anything
    # else has to be encoded, since an m4a container won't hold raw PCM.
    if audio_codec(src) == "aac":
        codec_args = ["-c", "copy"]
        how = "stream copy"
    else:
        codec_args = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "64k"]
        how = "re-encode"

    log(f"  splitting into ~{seconds // 60} minute chunks ({how}) ...")
    _ffmpeg(
        ["-i", str(src), "-f", "segment", "-segment_time", str(seconds),
         "-reset_timestamps", "1", *codec_args, pattern],
        f"splitting {src.name}",
    )
    chunks = sorted(chunk_dir.glob("chunk_*.m4a"))
    if not chunks:
        raise RuntimeError(f"splitting {src.name} produced no chunks")
    log(f"  {len(chunks)} chunks: " + ", ".join(_mb(_size(c)) for c in chunks))
    return chunks


def transcribe(
    path: str | Path,
    on_progress=None,
    provider: TranscriptionProvider | None = None,
) -> str:
    """Transcribe an audio file, compressing and splitting as needed.

    Two separate limits force a split, and both belong to the provider: the
    request size cap, and the output token cap that makes some models truncate
    long audio without raising. Which one binds depends on the model. On
    gpt-4o-mini-transcribe duration binds in practice; on Deepgram neither
    does, and a whole lecture goes up in one request.

    on_progress, if given, is called with a short human-readable string as the
    work moves along. A 75 minute lecture takes many minutes and, on the
    default provider, ten API calls, so something has to be able to say how
    far in it is.
    """
    def progress(detail: str) -> None:
        if on_progress:
            try:
                on_progress(detail)
            except Exception:
                # Reporting progress must never break a transcription.
                pass

    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"no such audio file: {src}")

    provider = provider or providers.get()
    total_seconds = duration_seconds(src)
    log(f"transcribing {src.name} ({_mb(_size(src))}"
        + (f", {total_seconds / 60:.0f} min" if total_seconds else "")
        + f") via {provider.name}")

    work_dir = Path(tempfile.mkdtemp(prefix=f"{src.stem}_", dir=config.WORK_DIR))
    try:
        audio = src
        if _size(audio) > provider.compress_threshold_bytes:
            progress("compressing the audio")
            audio = compress(audio, work_dir)

        seconds = duration_seconds(audio) or total_seconds
        too_long = seconds is not None and seconds > provider.max_chunk_seconds
        too_big = _size(audio) > provider.max_bytes

        if not (too_long or too_big):
            progress("transcribing")
            text = provider.transcribe_file(audio)
            log(f"  done: {len(text.split())} words")
            return text

        why = "duration" if too_long else "file size"
        log(f"  splitting on {why}")
        parts: list[str] = []
        progress("splitting the audio")
        chunks = split(audio, work_dir, provider.max_chunk_seconds)
        for i, chunk in enumerate(chunks, start=1):
            log(f"  chunk {i}/{len(chunks)} ...")
            progress(f"part {i} of {len(chunks)}")
            parts.append(_transcribe_chunk(chunk, provider, work_dir))

        text = "\n\n".join(p for p in parts if p)
        log(f"  done: {len(text.split())} words from {len(chunks)} chunks")
        return text
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _transcribe_chunk(
    chunk: Path, provider: TranscriptionProvider, work_dir: Path, depth: int = 0
) -> str:
    """Transcribe one chunk, halving it if the output looks truncated.

    A model that truncates gives no signal when it hits its output cap, so an
    implausibly long result is the only tell. Rather than lose the tail of a
    lecture, split that chunk and try again. A provider whose
    truncation_word_threshold is None cannot truncate, and skips all of this.

    Deliberately sends no `prompt`: passing the previous chunk's tail for
    continuity makes these models re-transcribe that text at the start of the
    next chunk. Measured on a 39 min lecture, it duplicated two of five seams
    and inflated the transcript by 13%.
    """
    text = provider.transcribe_file(chunk)

    threshold = provider.truncation_word_threshold
    if threshold is None or len(text.split()) < threshold:
        return text

    seconds = duration_seconds(chunk)
    if depth >= 2 or not seconds or seconds < 120:
        log(f"    WARNING: {len(text.split())} words from {chunk.name} may be "
            f"truncated; lower {provider.name}'s max_chunk_seconds in providers.py")
        return text

    log(f"    {len(text.split())} words looks truncated, re-splitting "
        f"{seconds / 60:.0f} min chunk in half")
    halves = split(chunk, work_dir, seconds=int(seconds // 2) + 1)
    out = [_transcribe_chunk(h, provider, work_dir, depth + 1) for h in halves]
    return "\n\n".join(t for t in out if t)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe a lecture recording. Transcript prints to stdout."
    )
    parser.add_argument("audio", help="path to .m4a / .mp3 / .wav")
    parser.add_argument(
        "--model", default=None,
        help=f"transcription provider, default {config.TRANSCRIBE_MODEL}. One of: "
             + ", ".join(sorted(providers.PROVIDERS)),
    )
    args = parser.parse_args(argv)

    try:
        print(transcribe(args.audio, provider=providers.get(args.model)))
    except Exception as exc:
        log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
