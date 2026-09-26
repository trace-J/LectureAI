"""Tests for intake/updates.py: which release counts, the daily check, the notice.

No network: GitHub's answer is a list handed in. From the project root:

    .venv/bin/python test_updates.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()

from intake import updates  # noqa: E402


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


RELEASES = [
    {"tag_name": "ffmpeg-8.1.2-1", "draft": False, "prerelease": False,
     "html_url": "https://github.com/SyllabusAI/LectureAI/releases/tag/ffmpeg-8.1.2-1"},
    {"tag_name": "v0.3.0", "draft": False, "prerelease": True, "html_url": "x"},
    {"tag_name": "v0.2.1", "draft": True, "prerelease": False, "html_url": "x"},
    {"tag_name": "v0.2.0", "draft": False, "prerelease": False,
     "html_url": "https://github.com/SyllabusAI/LectureAI/releases/tag/v0.2.0",
     "assets": [{"name": "Syllabus-0.2.0.dmg.sha256", "browser_download_url": "https://x/s"},
                {"name": "Syllabus-0.2.0.dmg", "browser_download_url": "https://x/Syllabus-0.2.0.dmg"}]},
    {"tag_name": "v0.1.0", "draft": False, "prerelease": False, "html_url": "x"},
]


def test_versions():
    assert updates.parse_version("v0.2.0") == (0, 2, 0)
    assert updates.parse_version("0.10.3") == (0, 10, 3)
    assert updates.parse_version("ffmpeg-8.1.2-1") is None
    assert updates.parse_version("v1.2") is None
    assert updates.is_newer("0.2.0", "0.1.9") and updates.is_newer("v0.10.0", "0.9.9")
    assert not updates.is_newer("0.2.0", "0.2.0") and not updates.is_newer("junk", "0.1.0")


def test_pick_release():
    best = updates.pick_release(RELEASES)
    assert best["tag_name"] == "v0.2.0", "skips ffmpeg, the prerelease, and the draft"
    assert updates._download_url(best) == "https://x/Syllabus-0.2.0.dmg"
    assert updates._download_url({"html_url": "page"}) == "page", "no dmg: the release page"
    assert updates.pick_release([RELEASES[0]]) is None
    assert updates.pick_release([]) is None
    assert updates.pick_release(list(reversed(RELEASES)))["tag_name"] == "v0.2.0", "order does not decide"


def test_check_writes_and_throttles():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "w" / "update.json"
        calls = []

        def fetch():
            calls.append(1)
            return RELEASES

        rec = updates.check(now=1000.0, fetch=fetch, path=path)
        assert rec["version"] == "0.2.0" and rec["url"].endswith(".dmg") and rec["error"] == "", rec
        assert json.loads(path.read_text())["version"] == "0.2.0"
        # Within a day: the file answers, GitHub is not asked again.
        rec = updates.check(now=1000.0 + 3600, fetch=fetch, path=path)
        assert len(calls) == 1 and rec["version"] == "0.2.0"
        # A day later: asked again.
        updates.check(now=1000.0 + updates.INTERVAL_SECONDS + 1, fetch=fetch, path=path)
        assert len(calls) == 2
        # force asks regardless.
        updates.check(now=1000.0 + updates.INTERVAL_SECONDS + 2, fetch=fetch, path=path, force=True)
        assert len(calls) == 3


def test_check_keeps_the_last_answer_on_failure():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "update.json"
        updates.check(now=1.0, fetch=lambda: RELEASES, path=path)

        def broken():
            raise OSError("offline")

        rec = updates.check(now=2.0, fetch=broken, path=path, force=True)
        assert rec["version"] == "0.2.0", "the previous answer survives"
        assert "offline" in rec["error"], rec
        assert rec["checked_at"] == 2.0
        # A first check that fails leaves a record with no version and no notice.
        empty = Path(tmp) / "empty.json"
        rec = updates.check(now=3.0, fetch=broken, path=empty)
        assert rec.get("version") in (None, "") and "offline" in rec["error"]
        assert updates.status(rec, current="0.1.0")["available"] is False


def test_status_for_each_install():
    rec = {"checked_at": 1000.0, "version": "0.2.0", "url": "https://x/Syllabus-0.2.0.dmg", "error": ""}
    s = updates.status(rec, current="0.1.0", frozen=True)
    assert s["available"] and s["version"] == "0.2.0" and s["how"] == "download", s
    assert s["url"] == "https://x/Syllabus-0.2.0.dmg" and s["current"] == "0.1.0"
    assert s["checked_at"].startswith("1970-01-01T00:16:40")
    s = updates.status(rec, current="0.1.0", frozen=False)
    assert s["how"] == "pipx" and s["available"]
    # Already current, or ahead (a dev checkout): nothing to say.
    s = updates.status(rec, current="0.2.0", frozen=True)
    assert not s["available"] and s["version"] == "" and s["url"] == "", s
    s = updates.status(rec, current="0.3.0", frozen=True)
    assert not s["available"]
    # No file yet.
    s = updates.status({}, current="0.1.0")
    assert not s["available"] and s["checked_at"] == "" and s["error"] == ""


def test_panel_status_carries_it():
    from intake import gui
    d = gui.app.test_client().get("/api/status").get_json()
    assert "update" in d and d["update"]["available"] is False, d["update"]
    assert d["update"]["current"] == updates.__version__


if __name__ == "__main__":
    results = [
        run("version tags parse and compare", test_versions),
        run("the newest published app release is picked", test_pick_release),
        run("the check writes its file and asks once a day", test_check_writes_and_throttles),
        run("a failed check keeps the last answer", test_check_keeps_the_last_answer_on_failure),
        run("the notice for the app and for pipx", test_status_for_each_install),
        run("the panel's status carries the notice", test_panel_status_carries_it),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
