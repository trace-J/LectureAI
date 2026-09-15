"""Syllabus as a Mac app: the same panel, in a window of its own.

    intake app              # the panel in a window instead of a browser tab
    intake app --port 5199  # against a different port

Nothing about the pipeline changes here. This module starts the panel from
gui.py on a thread, waits for it to answer, and puts a pywebview window
(WKWebView, the system's web engine) in front of it. The window has the
main thread, because macOS gives windows to nothing else; the panel, the
relay socket, and the recorder run exactly as they do under `intake panel`.

When a panel is already listening on the port, from `intake panel`, a
launchd agent, or another copy of the app, this one starts no server: it
opens its window on the panel that is running and leaves everything else
alone. Two panels against one home would fight over the microphone and the
watcher, so attaching is the only sane thing to do. If the port is held by
something that is not a Syllabus panel, it says so and exits.

Closing the window ends this process. A recording in progress is not
affected: ffmpeg runs in its own session and the next panel picks it up,
and the watcher is detached the same way. Keeping the app resident behind a
menu bar item is the next step, not this one.

Syllabus.app is this module frozen with PyInstaller; see packaging/. From a
checkout or a pipx install it needs the `app` extra: pip install '.[app]'.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.request
from typing import Callable

from intake import config, service

HOST = "127.0.0.1"

# A comfortable size for the dashboard's grid; the pages already lay
# themselves out for anything from a phone up.
WINDOW_SIZE = (1180, 820)
MIN_SIZE = (720, 520)

# How long to give the panel to bind before giving up on it.
STARTUP_SECONDS = 15.0


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def title() -> str:
    """The window's title: the product's name, nothing after it."""
    return config.PROFILE.title


def probe(port: int, host: str = HOST, timeout: float = 1.5) -> dict | None:
    """The status a Syllabus panel on `port` reports, or None if there is none.

    A panel is recognized by the shape of its status answer, not by the port
    being open: anything can hold a port. The same request the page polls
    every few seconds, so asking costs the running panel nothing new.
    """
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status",
                                    timeout=timeout) as response:
            data = json.load(response)
    except Exception:
        return None
    if isinstance(data, dict) and "watcher" in data and "configured" in data:
        return data
    return None


def page_for(status: dict | None) -> str:
    """Which page to open on a running panel: Setup until it is configured."""
    return "" if status and status.get("configured") else "setup"


def plan(port: int, host: str = HOST,
         probe_panel: Callable[..., dict | None] = probe,
         port_busy: Callable[..., bool] = service.port_answers) -> tuple[str, str]:
    """Decide what this launch does: ("attach", url), ("serve", url), or ("busy", why).

    Pure apart from the two probes, so the decision can be tested without a
    server on either side of it.
    """
    status = probe_panel(port, host)
    if status is not None:
        return "attach", f"http://{host}:{port}/{page_for(status)}"
    if port_busy(port):
        return "busy", (f"port {port} is in use by something that is not a "
                        f"{config.PROFILE.title} panel; stop it or choose another "
                        f"port with --port")
    return "serve", ""


def start_panel(port: int, host: str = HOST, wait: float = STARTUP_SECONDS) -> str:
    """Run the panel on a thread and return the page to open once it answers."""
    from intake import gui
    url = gui.prepare(host, port)
    threading.Thread(target=gui.serve, args=(host, port), name="panel",
                     daemon=True).start()
    if not service.wait_for_port(port, wait):
        raise RuntimeError(f"the panel did not start listening on port {port} "
                           f"within {wait:.0f} seconds")
    return url


def open_window(url: str) -> None:
    """Show `url` in the app's window and block until the window is closed."""
    import webview  # pywebview; imported here so the CLI never needs it
    webview.create_window(title(), url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                          min_size=MIN_SIZE)
    webview.start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="intake app",
        description=f"{config.PROFILE.title} in a window of its own.")
    parser.add_argument("--port", type=int, default=config.PROFILE.panel_port,
                        help=f"{config.PROFILE.panel_port} for this profile")
    args = parser.parse_args(argv)

    action, detail = plan(args.port)
    if action == "busy":
        log(f"{config.PROFILE.title}: {detail}")
        return 1
    if action == "attach":
        log(f"{config.PROFILE.title}: a panel is already running on port {args.port}; "
            f"opening a window on it and starting nothing")
        url = detail
    else:
        try:
            url = start_panel(args.port)
        except RuntimeError as exc:
            log(f"{config.PROFILE.title}: {exc}")
            return 1
    open_window(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
