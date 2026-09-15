"""Where ffmpeg and ffprobe are: the copies inside Syllabus.app first, then PATH.

The pipeline calls both by name and used to rely on Homebrew for them, which
is one of the three Terminal steps the app exists to remove. Syllabus.app
carries its own static builds (see packaging/ffmpeg/), so inside the bundle
they are found there before anything else. From a checkout or a pipx
install the bundle directory does not exist and PATH decides, as before.

    ffmpeg()          # the path to run, or "ffmpeg" if none is installed
    find("ffprobe")   # the path, or None

$INTAKE_FFMPEG_DIR names a folder to look in ahead of PATH, for a checkout
that wants to run against a downloaded build without installing it.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FFMPEG_DIR_ENV_VAR = "INTAKE_FFMPEG_DIR"

# Inside the frozen bundle, PyInstaller unpacks collected files next to the
# code and names that folder in sys._MEIPASS. The build (packaging/syllabus.spec)
# puts both binaries and their license texts in a folder called ffmpeg there.
BUNDLED_SUBDIR = "ffmpeg"


def bundle_dir() -> Path | None:
    """The folder of bundled tools inside Syllabus.app, or None outside it."""
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        return None
    return Path(root) / BUNDLED_SUBDIR


def search_dirs(env: dict | None = None) -> list[Path]:
    """Folders consulted before PATH, in order."""
    source = os.environ if env is None else env
    dirs: list[Path] = []
    bundled = bundle_dir()
    if bundled is not None:
        dirs.append(bundled)
    extra = (source.get(FFMPEG_DIR_ENV_VAR) or "").strip()
    if extra:
        dirs.append(Path(extra).expanduser())
    return dirs


def find(name: str, env: dict | None = None) -> str | None:
    """The full path of `name`, bundled copy first, then $INTAKE_FFMPEG_DIR, then PATH."""
    for folder in search_dirs(env):
        candidate = folder / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(name)


def ffmpeg() -> str:
    """What to run for ffmpeg. The bare name when nothing is found, so the
    caller's error still says ffmpeg."""
    return find("ffmpeg") or "ffmpeg"


def ffprobe() -> str:
    return find("ffprobe") or "ffprobe"


def bundled() -> bool:
    """Whether the ffmpeg in use is the one inside Syllabus.app."""
    folder = bundle_dir()
    path = find("ffmpeg")
    return bool(folder and path and Path(path).parent == folder)


def install_hint() -> str:
    """The fix to show when ffmpeg is missing, for where this is running."""
    if bundle_dir() is not None:
        return "the copy inside Syllabus.app is missing; download Syllabus again"
    return "brew install ffmpeg"
