"""Tests for watch.process(), the orchestrator that turns one recording into
filed notes.

Nothing here reaches a network, a provider, or Google Drive: transcription,
summarization and the upload are replaced with fakes that record what they
were asked to do. The files are real, inside a throwaway $INTAKE_HOME. From
the project root:

    .venv/bin/python test_watch.py

process() had no coverage at all before this file, which is how a handful of
defects in it went unnoticed by a green suite. It is the one place a lecture
can be lost, paid for twice, or filed under the wrong class, so the fakes
below count calls as well as returning values: several of the things worth
asserting here are about how often an expensive step runs, not about what it
returns.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config  # noqa: E402
from intake import notion_tasks, summarize, transcribe, watch  # noqa: E402
from intake import upload as drive  # noqa: E402

# Tuesday. The sample schedule puts ACCT-4321 at 14:00 and ENTR-3306 at 12:00,
# and no class meets on a Sunday, which is how the "no class matches" cases
# below get a timestamp that cannot accidentally hit one.
TUESDAY_2PM_END = "2026-09-15 15:00"   # a 60 minute class ending at 15:00
SUNDAY = "2026-09-13 15:00"


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


def when(text: str) -> float:
    """'2026-09-15 15:00' as an mtime."""
    from datetime import datetime
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


class Fakes:
    """Stands in for everything process() would otherwise spend money on.

    Installed by replacing attributes on the real modules rather than on
    `watch`, because process() reaches them through the module object at call
    time. restore() puts the originals back so one test cannot leak into the
    next.
    """

    def __init__(self, *, transcript="hello there lecture", duration=3600.0,
                 topic="Theories-Of-Leadership", actions=None, terms=None,
                 upload_error=None, notion=False):
        self.transcript = transcript
        self.duration = duration
        self.topic = topic
        self.actions = actions if actions is not None else []
        self.terms = terms if terms is not None else []
        self.upload_error = upload_error
        self.notion = notion
        self.transcribed = 0
        self.summarized = 0
        self.uploads = []       # one dict per drive.upload call
        self._saved = {}

    def _remember(self, module, name, value):
        self._saved.setdefault((module, name), getattr(module, name))
        setattr(module, name, value)

    def install(self):
        def fake_duration(path):
            return self.duration

        def fake_transcribe(path, on_progress=None):
            self.transcribed += 1
            if on_progress:
                on_progress("working")
            return self.transcript

        def fake_summarize(text, course, date):
            self.summarized += 1
            return {
                "summary_md": f"# {course} {date}\n\nnotes",
                "topic_slug": self.topic,
                "key_terms": list(self.terms),
                "action_items": list(self.actions),
            }

        def fake_render(result, course, date):
            return result["summary_md"]

        def fake_upload(local_path, course, interactive=True, *, subfolder="",
                        as_google_doc=False, name=None, recording_key=None,
                        time_suffix=""):
            call = {
                "path": Path(local_path), "course": course,
                "subfolder": subfolder, "as_google_doc": as_google_doc,
                "name": name, "recording_key": recording_key,
                "time_suffix": time_suffix,
                "existed": Path(local_path).is_file(),
                "body": Path(local_path).read_text() if Path(local_path).is_file() else None,
            }
            self.uploads.append(call)
            if self.upload_error and len(self.uploads) == self.upload_error[0]:
                raise RuntimeError(self.upload_error[1])
            final = name or Path(local_path).name
            return drive.Upload(url=f"https://drive.test/{final}", name=final)

        self._remember(transcribe, "duration_seconds", fake_duration)
        self._remember(transcribe, "transcribe", fake_transcribe)
        self._remember(summarize, "summarize", fake_summarize)
        self._remember(summarize, "render_markdown", fake_render)
        self._remember(drive, "upload", fake_upload)
        self._remember(notion_tasks, "enabled", lambda: self.notion)
        return self

    def restore(self):
        for (module, name), value in self._saved.items():
            setattr(module, name, value)
        self._saved.clear()

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.restore()
        return False


def recording(name="ACCT-4321_2026-09-15_1400.m4a", *, at=TUESDAY_2PM_END,
              body="AUDIO", where=None):
    """Put a fake recording in the inbox with a chosen mtime."""
    import os
    directory = where if where is not None else config.INBOX_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body)
    stamp = when(at)
    os.utime(path, (stamp, stamp))
    return path


def log_lines():
    if not config.LOG_FILE.is_file():
        return []
    return [line.split("\t") for line in
            config.LOG_FILE.read_text().splitlines() if line.strip()]


def clear():
    """Empty the directories process() writes to, between tests."""
    for directory in (config.INBOX_DIR, config.PROCESSED_DIR):
        if directory.is_dir():
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink()
    config.LOG_FILE.unlink(missing_ok=True)


results = []


# 1. The ordinary case, end to end: one class recording becomes a summary and
#    a transcript in Drive, a line in pipeline.log, and no leftovers on disk.
def t1():
    clear()
    audio = recording()
    with Fakes() as fake:
        out = watch.process(audio, interactive=False)
    assert out["course"] == "ACCT-4321", out
    assert out["date"] == "2026-09-15", out
    assert fake.transcribed == 1, fake.transcribed
    assert fake.summarized == 1, fake.summarized
    assert len(fake.uploads) == 2, fake.uploads
    assert out["summary_url"].startswith("https://drive.test/"), out
results.append(run("a scheduled recording is transcribed, summarized and filed", t1))


# 2. Both staged copies are written before the upload and removed after it, so
#    a finished lecture leaves processed/ clean.
def t2():
    clear()
    audio = recording()
    with Fakes() as fake:
        watch.process(audio, interactive=False)
    assert all(call["existed"] for call in fake.uploads), fake.uploads
    leftovers = sorted(p.name for p in config.PROCESSED_DIR.glob("*"))
    assert leftovers == [], leftovers
results.append(run("staged copies exist for the upload and are cleared after", t2))


# 3. The summary goes to the course folder, the transcript one level down, and
#    the transcript takes its name from whatever the summary ended up called:
#    a renamed pair has to stay a pair.
def t3():
    clear()
    audio = recording()
    with Fakes() as fake:
        watch.process(audio, interactive=False)
    summary, transcript = fake.uploads
    assert summary["subfolder"] == "", summary
    assert transcript["subfolder"] == config.TRANSCRIPT_SUBFOLDER, transcript
    assert transcript["name"] == f"{summary['name']}.txt", (summary, transcript)
results.append(run("transcript is filed under the summary's final name", t3))


# 4. Both uploads carry the recording's identity, which is what lets a re-run
#    replace its own past output instead of a different lecture's.
def t4():
    clear()
    audio = recording()
    with Fakes() as fake:
        watch.process(audio, interactive=False)
    key = config.recording_key(when(TUESDAY_2PM_END), 3600.0)
    suffix = config.recording_time_suffix(when(TUESDAY_2PM_END), 3600.0)
    for call in fake.uploads:
        assert call["recording_key"] == key, (call, key)
        assert call["time_suffix"] == suffix, (call, suffix)
results.append(run("both uploads carry the recording key and time suffix", t4))


# 5. The seventh field the panel's dashboard reads.
def t5():
    clear()
    audio = recording()
    with Fakes(transcript="one two three", actions=["a", "b"], terms=["x"]):
        watch.process(audio, interactive=False)
    line = log_lines()[-1]
    measures = json.loads(line[6])
    assert measures["seconds"] == 3600, measures
    assert measures["words"] == 3, measures
    assert measures["actions"] == 2, measures
    assert measures["terms"] == 1, measures
results.append(run("the log line carries seconds, words, actions and terms", t5))


# 6. A recording whose timestamp matches no class falls back to the course in
#    its filename, which is what saves a file copied off a phone.
def t6():
    clear()
    audio = recording("ENTR-3306_2026-09-13_1400.m4a", at=SUNDAY)
    with Fakes():
        out = watch.process(audio, interactive=False)
    assert out["course"] == "ENTR-3306", out
results.append(run("no class at that hour falls back to the filename", t6))


# 7. Neither signal identifies a course, so it files under UNKNOWN rather than
#    guessing or refusing.
def t7():
    clear()
    audio = recording("voice-memo-4.m4a", at=SUNDAY)
    with Fakes():
        out = watch.process(audio, interactive=False)
    assert out["course"] == config.UNKNOWN_COURSE, out
results.append(run("an unidentifiable recording files under UNKNOWN", t7))


# 8. A Drive failure must not cost the recording: the original stays where it
#    was so the file can be tried again.
def t8():
    clear()
    audio = recording()
    with Fakes(upload_error=(1, "drive is down")) as fake:
        try:
            watch.process(audio, interactive=False)
        except RuntimeError as exc:
            assert "drive is down" in str(exc), exc
        else:
            raise AssertionError("the upload failure was swallowed")
    assert audio.is_file(), "the original recording was lost on an upload failure"
    assert fake.transcribed == 1, fake.transcribed
results.append(run("an upload failure leaves the original recording in place", t8))


# 9. The expensive output survives that same failure on disk. (Whether the
#    retry actually reuses it is a separate question, and today it does not.)
def t9():
    clear()
    audio = recording()
    with Fakes(upload_error=(1, "drive is down")):
        try:
            watch.process(audio, interactive=False)
        except RuntimeError:
            pass
    staged = sorted(p.suffix for p in config.PROCESSED_DIR.glob("*"))
    assert staged == [".md", ".txt"], staged
results.append(run("a failed upload leaves the transcript and summary staged", t9))


# 10. With the shipped default the original is deleted once both uploads land.
def t10():
    clear()
    audio = recording()
    with Fakes():
        out = watch.process(audio, interactive=False)
    assert not audio.exists(), "the original should be gone after a clean run"
    assert out["original"] == "(deleted)", out
results.append(run("the original is deleted after a clean run", t10))


# 11. With the archive setting instead, the original lands in processed/.
def t11():
    clear()
    audio = recording()
    keep = config.DELETE_ORIGINAL_AFTER_UPLOAD
    config.DELETE_ORIGINAL_AFTER_UPLOAD = False
    try:
        with Fakes():
            out = watch.process(audio, interactive=False)
    finally:
        config.DELETE_ORIGINAL_AFTER_UPLOAD = keep
    archived = config.PROCESSED_DIR / audio.name
    assert archived.is_file(), sorted(p.name for p in config.PROCESSED_DIR.glob("*"))
    assert out["original"] == str(archived), out
results.append(run("archiving keeps the original in processed/", t11))


# 12. Notion is off, so nothing is pushed, but the action items are still
#     counted: the panel has to be able to show what was found.
def t12():
    clear()
    audio = recording()
    with Fakes(actions=["read chapter 4"], notion=False):
        watch.process(audio, interactive=False)
    line = log_lines()[-1]
    assert line[5] == "", f"a Notion warning appeared with Notion off: {line[5]!r}"
    assert json.loads(line[6])["actions"] == 1, line
results.append(run("action items are counted with Notion switched off", t12))


# 13. A path that is not a file is refused before anything is spent on it.
def t13():
    clear()
    missing = config.INBOX_DIR / "not-here.m4a"
    with Fakes() as fake:
        try:
            watch.process(missing, interactive=False)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("a missing file was accepted")
    assert fake.transcribed == 0, fake.transcribed
results.append(run("a missing file is refused before transcription", t13))


# 14. The status file is what the panel polls while a lecture is in flight;
#     it has to name the stage and the course, not just say "busy".
def t14():
    clear()
    audio = recording()
    seen = []
    with Fakes() as fake:
        original = watch.write_status

        def spy(stage, file="", course="", detail="", started=""):
            seen.append((stage, course))
            return original(stage, file, course, detail, started)

        watch.write_status = spy
        try:
            watch.process(audio, interactive=False)
        finally:
            watch.write_status = original
    stages = [stage for stage, _ in seen]
    assert "transcribing" in stages, stages
    assert "summarizing" in stages, stages
    assert "uploading" in stages, stages
    assert all(course == "ACCT-4321" for _, course in seen), seen
results.append(run("progress is reported for each stage with the course", t14))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
