"""Tests for intake/app.py and packaging/entry.py: what a launch decides to do.

No window is opened, no server is started by the app itself. A panel is
stood up on an ephemeral port only to check that the probe recognizes one.
From the project root:

    .venv/bin/python test_app.py
"""
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "packaging"))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from werkzeug.serving import make_server  # noqa: E402

import entry  # noqa: E402  packaging/entry.py
from intake import app, gui  # noqa: E402


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    except Exception as exc:
        print(f"FAIL  {label}\n      unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"ok    {label}")
    return True


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_finds_a_panel():
    server = make_server("127.0.0.1", 0, gui.app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status = app.probe(port)
        assert status is not None, "a running panel was not recognized"
        assert status["configured"] is False, "the test home has no keys"
        assert app.page_for(status) == "setup"
        action, url = app.plan(port)
        assert action == "attach", action
        assert url == f"http://127.0.0.1:{port}/setup", url
    finally:
        server.shutdown()


def test_probe_ignores_a_closed_port():
    port = free_port()
    assert app.probe(port) is None
    action, detail = app.plan(port)
    assert action == "serve", (action, detail)


def test_probe_ignores_something_else():
    # A listener that answers nothing a panel would.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        port = s.getsockname()[1]
        assert app.probe(port, timeout=0.5) is None
        action, detail = app.plan(port)
        assert action == "busy", action
        assert str(port) in detail and "--port" in detail, detail


def test_page_for():
    assert app.page_for(None) == "setup"
    assert app.page_for({"configured": False}) == "setup"
    assert app.page_for({"configured": True}) == ""


def test_title_and_size():
    assert app.title() == "Syllabus"
    assert app.WINDOW_SIZE[0] >= app.MIN_SIZE[0]
    assert app.WINDOW_SIZE[1] >= app.MIN_SIZE[1]


def test_entry_dispatch():
    seen = {}

    def fake(name, code):
        def call(args):
            seen[name] = args
            return code
        return call

    code = entry.main([], run_cli=fake("cli", 9), run_app=fake("app", 3))
    assert seen == {"app": []}, seen
    assert code == 3, code

    seen.clear()
    code = entry.main(["doctor", "--help"], run_cli=fake("cli", 7), run_app=fake("app", 3))
    assert seen == {"cli": ["doctor", "--help"]}, seen
    assert code == 7, code


def test_entry_pins_the_profile():
    import os
    from intake import profiles
    os.environ.pop(profiles.PROFILE_ENV_VAR, None)
    entry.main([], run_app=lambda a: 0)
    assert os.environ[profiles.PROFILE_ENV_VAR] == "syllabus"


if __name__ == "__main__":
    results = [
        run("probe recognizes a running panel and attaches to it", test_probe_finds_a_panel),
        run("a closed port means start our own panel", test_probe_ignores_a_closed_port),
        run("a port held by something else is reported, not attached", test_probe_ignores_something_else),
        run("setup page until configured", test_page_for),
        run("window title and size", test_title_and_size),
        run("entry: no arguments is the app, arguments are the command", test_entry_dispatch),
        run("entry pins the syllabus profile", test_entry_pins_the_profile),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
