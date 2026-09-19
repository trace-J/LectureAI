"""Tests for assistant.py: the library, the two stages, and the instrumentation.

Nothing here talks to Drive or to Anthropic. The Drive service is a stub that
answers from a dict, and the model is never called: what is tested is the
context this module builds and the bookkeeping it keeps, which is where the
cost model lives and where the bugs were.

    .venv/bin/python test_assistant.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import assistant, config  # noqa: E402

LOG = (
    "2026-09-15T10:00:00\tACCT-4321\ta.m4a\tACCT-4321_2026-09-15_Process-Costing"
    '\thttps://docs.google.com/document/d/1aBcProcessCosting15xyzQ/edit\t\t{"terms":9,"actions":1}\n'
    "2026-09-17T10:00:00\tACCT-4321\tb.m4a\tACCT-4321_2026-09-17_Cost-Volume-Profit"
    '\thttps://docs.google.com/document/d/1aBcCostVolumeProfit17xyQ/edit\t\t{"terms":4,"actions":0}\n'
    "2026-09-18T10:00:00\tRELI-3304\tc.m4a\tRELI-3304_2026-09-18_Inspiration"
    "\thttps://docs.google.com/document/d/1aBcInspiration18xyzabcQ/edit\n"
    "2026-09-18T11:00:00\tERROR\td.m4a\tsomething went wrong\n"
)


class FakeFiles:
    """Just enough of Drive's files() for the two stages."""

    def __init__(self, store, native, calls):
        self.store, self.native, self.calls = store, native, calls

    def _exec(self, value):
        class _R:
            def execute(_):
                if isinstance(value, Exception):
                    raise value
                return value
        return _R()

    def get(self, fileId, fields=None):
        self.calls.append(("get", fileId))
        if fileId not in self.store:
            return self._exec(RuntimeError(f"no such file {fileId}"))
        kind = ("application/vnd.google-apps.document"
                if fileId in self.native else "text/markdown")
        return self._exec({"mimeType": kind})

    def export(self, fileId, mimeType):
        self.calls.append(("export", fileId))
        if fileId not in self.native:
            return self._exec(RuntimeError("Export only supports Docs Editors files."))
        return self._exec(self.store[fileId].encode())

    def get_media(self, fileId):
        self.calls.append(("get_media", fileId))
        return self._exec(self.store[fileId].encode())

    def list(self, **kw):
        self.calls.append(("list", kw.get("q", "")))
        found = [{"id": k, "name": k} for k in self.store
                 if k.startswith("TRANSCRIPT:") and k[11:] in kw.get("q", "")]
        return self._exec({"files": found})


class FakeService:
    def __init__(self, store, native=(), calls=None):
        self.calls = [] if calls is None else calls
        self._files = FakeFiles(store, set(native) or set(store), self.calls)

    def files(self):
        return self._files


def setup(log=LOG):
    """A fresh home with a known log and an empty summary cache."""
    home = fresh_home()
    config.HOME_DIR = config.BASE_DIR = home
    config.LOG_FILE = home / "pipeline.log"
    config.LOG_FILE.write_text(log)
    cache = home / ".assistant"
    if cache.exists():
        for f in cache.iterdir():
            f.unlink()
    log_file = home / "assistant.log"
    if log_file.exists():
        log_file.unlink()
    return home


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


results = []


def t1():
    setup()
    lecs = assistant.library()
    assert len(lecs) == 3, f"expected 3 lectures, got {len(lecs)}"
    # Newest first, and the ERROR line is not a lecture.
    assert lecs[0].date == "2026-09-18", lecs[0].date
    assert all(l.course != "ERROR" for l in lecs), "an error line became a lecture"
results.append(run("the library reads filed lectures and skips errors", t1))


def t2():
    setup()
    lec = [l for l in assistant.library() if l.name.endswith("Process-Costing")][0]
    assert lec.doc_id == "1aBcProcessCosting15xyzQ", lec.doc_id
    assert lec.topic == "Process Costing", lec.topic
    assert lec.title == "ACCT-4321 2026-09-15: Process Costing", lec.title
results.append(run("a lecture knows its doc id, topic, and citable title", t2))


def t3():
    setup()
    found = assistant.courses()
    assert [c["course"] for c in found] == ["ACCT-4321", "RELI-3304"], found
    assert found[0]["lectures"] == 2, found[0]
results.append(run("courses are listed by how many lectures they have", t3))


def t4():
    setup()
    svc = FakeService({"1aBcProcessCosting15xyzQ": "costing summary", "1aBcCostVolumeProfit17xyQ": "cvp summary"})
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks = assistant.stage_one(svc, lecs)
    assert len(blocks) == 2, f"expected 2 documents, got {len(blocks)}"
    assert all(b["citations"]["enabled"] for b in blocks), "citations are off"
    # The breakpoint goes on the last block and nowhere else, or the whole
    # prefix is not actually cached.
    cached = [i for i, b in enumerate(blocks) if "cache_control" in b]
    assert cached == [len(blocks) - 1], f"cache breakpoints at {cached}"
results.append(run("stage one builds cited documents with one cache breakpoint", t4))


def t5():
    # The bug that took the whole course down: a lecture filed before the
    # pipeline converted summaries to Docs is still text/markdown, and
    # export() refuses it.
    setup()
    svc = FakeService({"1aBcProcessCosting15xyzQ": "costing summary", "1aBcCostVolumeProfit17xyQ": "markdown summary"},
                      native=["1aBcProcessCosting15xyzQ"])
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks = assistant.stage_one(svc, lecs)
    assert len(blocks) == 2, f"a markdown summary was dropped: {len(blocks)} blocks"
    assert any("markdown summary" in b["source"]["data"] for b in blocks), blocks
results.append(run("a summary that is not a Doc is downloaded, not skipped", t5))


def t6():
    # One unreadable lecture must cost the student that lecture, not the course.
    setup()
    svc = FakeService({"1aBcProcessCosting15xyzQ": "costing summary"})     # 1aBcCostVolumeProfit17xyQ missing entirely
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks = assistant.stage_one(svc, lecs)
    assert len(blocks) == 1, f"expected the readable one, got {len(blocks)}"
results.append(run("an unreadable lecture is skipped, not raised", t6))


def t7():
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s"}
    calls = []
    svc = FakeService(store, calls=calls)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    assistant.stage_one(svc, lecs)
    first = len([c for c in calls if c[0] in ("export", "get_media")])
    assistant.stage_one(svc, lecs)
    second = len([c for c in calls if c[0] in ("export", "get_media")])
    assert second == first, "the second read went back to Drive instead of the cache"
results.append(run("a summary is fetched from Drive once and then cached", t7))


def t8():
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s",
             "TRANSCRIPT:ACCT-4321_2026-09-15_Process-Costing": "the full words"}
    svc = FakeService(store)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks, used = assistant.stage_two(
        svc, lecs, ["ACCT-4321 2026-09-15: Process Costing"])
    assert used == ["ACCT-4321 2026-09-15: Process Costing"], used
    assert "the full words" in blocks[0]["source"]["data"], blocks
results.append(run("stage two fetches the transcript the model named", t8))


def t9():
    # The model quotes a title back at us rather than echoing an id, so a near
    # miss still has to find the lecture.
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s",
             "TRANSCRIPT:ACCT-4321_2026-09-15_Process-Costing": "the full words"}
    svc = FakeService(store)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    _, used = assistant.stage_two(svc, lecs, ["acct-4321 2026-09-15: process costing"])
    assert len(used) == 1, f"a case-different title did not match: {used}"
results.append(run("a transcript is matched on a loosely quoted title", t9))


def t10():
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s"}
    svc = FakeService(store)
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    blocks, used = assistant.stage_two(svc, lecs, ["No Such Lecture"])
    assert blocks == [] and used == [], (blocks, used)
results.append(run("a transcript that cannot be found yields nothing, not an error", t10))


def t11():
    # The escalation cap is what stands in for the entitlement that is not
    # built yet, so it has to actually hold.
    setup()
    store = {"1aBcProcessCosting15xyzQ": "s", "1aBcCostVolumeProfit17xyQ": "s"}
    lecs = [l for l in assistant.library() if l.course == "ACCT-4321"]
    for lec in lecs:
        store[f"TRANSCRIPT:{lec.name}"] = "x" * (assistant.MAX_TRANSCRIPT_CHARS)
    svc = FakeService(store)
    _, used = assistant.stage_two(svc, lecs, [l.title for l in lecs])
    assert len(used) == 1, f"the size cap let {len(used)} transcripts through"
results.append(run("the transcript size cap holds", t11))


def t12():
    setup()
    assistant.record_session("ACCT-4321", "a question", False, [], {"output_tokens": 10})
    assistant.record_session("ACCT-4321", "another", True, ["ACCT-4321 x"], {})
    rate = assistant.escalation_rate()
    assert rate == {"sessions": 2, "escalated": 1, "rate": 0.5}, rate
    line = json.loads((config.BASE_DIR / "assistant.log").read_text().splitlines()[0])
    assert line["course"] == "ACCT-4321" and line["escalated"] is False, line
results.append(run("every session is logged and the escalation rate is measured", t12))


def t13():
    setup()
    events = list(assistant.ask("anything", "PHYS-1000"))
    assert events[-1]["type"] == "error", events
    assert "PHYS-1000" in events[-1]["text"], events[-1]
    # Nothing was billed, so nothing is logged.
    assert assistant.escalation_rate()["sessions"] == 0, "an unasked question was logged"
results.append(run("asking about a course with no lectures never calls the model", t13))


def t14():
    setup()
    events = list(assistant.ask("   ", "ACCT-4321"))
    assert events == [{"type": "error", "text": "Ask a question first."}], events
results.append(run("an empty question is refused before anything is fetched", t14))


def t15():
    # The assistant's model is a costing decision; a silent change to Opus is
    # the difference between a 53% margin and a 29% one.
    assert config.ASSISTANT_MODEL == "claude-sonnet-5", config.ASSISTANT_MODEL
results.append(run("the assistant runs on Sonnet 5", t15))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
