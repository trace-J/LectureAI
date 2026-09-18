"""Tests for insights.py: the dashboard's numbers, counted from a fake log
against the sample schedule at a fixed moment in time.

    .venv/bin/python test_insights.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import SAMPLE_SCHEDULE, fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import config, insights  # noqa: E402


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
SCHEDULE = config.parse_schedule(SAMPLE_SCHEDULE, "schedule.toml")

# Saturday morning, September 12, 2026. The week ran Mon Sep 7 to Fri Sep 11.
NOW = datetime(2026, 9, 12, 10, 0)


def line(*fields):
    return "\t".join(fields)


def success(when, course, date, topic, warning=None, measures=None):
    fields = [when, course, f"{course}_{date}_0859.m4a", f"{course}_{date}_{topic}",
              f"https://docs.google.com/document/d/{course}-{date}/edit"]
    if warning is not None or measures is not None:
        fields.append(warning or "")
    if measures is not None:
        fields.append(json.dumps(measures, separators=(",", ":")))
    return line(*fields)


LOG = "\n".join([
    # Week of Aug 31: three of eight classes recorded, one of them measured.
    success("2026-08-31T10:40:00", "ENTR-4306", "2026-08-31", "Theories-Of-Leadership"),
    success("2026-09-01T15:50:00", "ACCT-4321", "2026-09-01", "Job-Order-Costing",
            measures={"seconds": 4500, "words": 9800, "actions": 3, "terms": 12}),
    line("2026-09-02T10:31:15", "ERROR", "corrupt.m4a", "Audio file might be corrupted"),
    success("2026-09-03T15:40:00", "ACCT-4321", "2026-09-03", "Process-Costing",
            warning="1 of 2 to-dos did not reach Notion: no week heading"),
    # This week: Tue and Thu ACCT recorded, the Tue ENTR-3306 too; Wed, Fri, Mon missed.
    success("2026-09-08T13:57:17", "ENTR-3306", "2026-09-08", "Technology-And-Brain-Drain"),
    success("2026-09-08T15:36:18", "ACCT-4321", "2026-09-08", "Job-Order-Costing-Flows"),
    # Processed on Friday, but the stem says the class was Thursday.
    success("2026-09-11T09:02:00", "ACCT-4321", "2026-09-10", "Activity-Based-Costing",
            measures={"seconds": 4200, "words": 9100, "actions": 1, "terms": 9}),
    # Filed under UNKNOWN: counts as a lecture, covers no class.
    line("2026-09-09T12:00:00", "UNKNOWN", "voice memo.m4a", "UNKNOWN_2026-09-09_Untitled",
         "https://docs.google.com/document/d/UNK/edit"),
]) + "\n"

ROWS = insights.parse_log(LOG)
OUT = insights.compute(ROWS, SCHEDULE, NOW)


def t1():
    assert len(ROWS) == 8, [r["name"] for r in ROWS]
    kinds = [r["error"] is None for r in ROWS]
    assert kinds.count(False) == 1, kinds
    measured = next(r for r in ROWS if r["name"].endswith("Job-Order-Costing"))
    assert measured["seconds"] == 4500 and measured["words"] == 9800, measured
    assert measured["actions"] == 3 and measured["terms"] == 12, measured
    assert measured["warning"] == "", measured
    plain = next(r for r in ROWS if r["name"].endswith("Theories-Of-Leadership"))
    assert plain["seconds"] == 0 and plain["words"] == 0, "a five-field line must read as unmeasured"
    warned = next(r for r in ROWS if r["name"].endswith("Process-Costing"))
    assert warned["warning"].startswith("1 of 2"), warned
results.append(run("five-, six-, and seven-field lines all parse, measurements included", t1))


def t2():
    late = next(r for r in ROWS if r["name"].endswith("Activity-Based-Costing"))
    assert late["when"].startswith("2026-09-11"), late
    assert late["date"] == "2026-09-10", "the class date must come from the stem, not the log time"
    error = next(r for r in ROWS if r["error"])
    assert error["date"] == "2026-09-02", error
results.append(run("a lecture is dated by its stem, so a late filing lands on the right day", t2))


def t3():
    for bad in ("not json", "[1,2]", '{"seconds": "lots"}', '{"seconds": -4}', '{"words": true}'):
        assert insights.parse_measures(bad) == {}, bad
    assert insights.parse_measures('{"seconds": 90.6, "extra": 1}') == {"seconds": 90}
results.append(run("a seventh field that is not our measurements is ignored, not raised", t3))


def t3b():
    # JSON has no infinity, but json.loads reads 1e999 as one, and int(inf)
    # raises. The raise came out of parse_log, which /api/status reaches
    # before anything else it returns, so one bad number in one old line
    # took down the recording state, the Record button, the watcher, the
    # inbox and the account card along with the chart.
    for bad in ('{"seconds":1e999}', '{"seconds":-1e999}',
                '{"words":1e999}', '{"seconds":1e400,"terms":2e999}'):
        assert insights.parse_measures(bad) == {}, bad
    # A bad measurement is dropped; the good ones on the same line survive.
    assert insights.parse_measures('{"seconds":1e999,"words":12}') == {"words": 12}
    # And an honest-but-absurd number is out of range rather than crashing.
    assert insights.parse_measures('{"seconds":1e308}') == {}
results.append(run("a measurement that is not finite is dropped, not raised", t3b))


def t4():
    codes = [c["code"] for c in OUT["courses"]]
    # Schedule order first (sorted), UNKNOWN last with no color slot.
    assert codes == ["ACCT-4321", "ENTR-3306", "ENTR-4306", "RELI-3304", "UNKNOWN"], codes
    slots = {c["code"]: c["slot"] for c in OUT["courses"]}
    assert slots["ACCT-4321"] == 0 and slots["RELI-3304"] == 3 and slots["UNKNOWN"] == -1, slots
    acct = OUT["courses"][0]
    assert acct["count"] == 4 and acct["last"] == "2026-09-10", acct
    assert acct["minutes"] == round((4500 + 4200) / 60) and acct["actions"] == 4, acct
    assert acct["meetings"] == 2, acct
    reli = OUT["courses"][3]
    assert reli["count"] == 0 and reli["last"] == "" and reli["meetings"] == 1, reli
results.append(run("courses come in schedule order with counts, minutes, and a color slot", t4))


def t5():
    weeks = OUT["weeks"]
    assert len(weeks) == insights.CHART_WEEKS, len(weeks)
    assert weeks[-1]["start"] == "2026-09-07" and weeks[-1]["current"], weeks[-1]
    assert weeks[-2]["start"] == "2026-08-31" and not weeks[-2]["current"], weeks[-2]
    assert weeks[-2]["counts"] == {"ENTR-4306": 1, "ACCT-4321": 2}, weeks[-2]
    assert weeks[-1]["counts"] == {"ENTR-3306": 1, "ACCT-4321": 2, "UNKNOWN": 1}, weeks[-1]
    assert weeks[-1]["total"] == 4
    # The schedule is only expected to be met from the first lecture's week on.
    assert weeks[-1]["scheduled"] == 8 and weeks[-2]["scheduled"] == 8, weeks[-2:]
    assert all(w["scheduled"] == 0 and w["total"] == 0 for w in weeks[:-2]), weeks[:-2]
results.append(run("lectures stack per week, with the schedule's target only once classes began", t5))


def t6():
    days = OUT["week"]["days"]
    assert [d["day"] for d in days] == ["Mon", "Tue", "Wed", "Thu", "Fri"], days
    assert OUT["week"]["start"] == "2026-09-07"
    by_day = {d["day"]: d for d in days}
    assert not any(d["today"] for d in days), "Saturday is not shown, so no day is today"
    tue = by_day["Tue"]["classes"]
    assert [c["course"] for c in tue] == ["ENTR-3306", "ACCT-4321"], tue
    assert all(c["recorded"] and not c["missed"] for c in tue), tue
    assert tue[1]["url"].endswith("ACCT-4321-2026-09-08/edit"), tue[1]
    wed = by_day["Wed"]["classes"][0]
    assert wed["course"] == "ENTR-4306" and wed["missed"] and not wed["recorded"], wed
    thu = by_day["Thu"]["classes"]
    assert thu[0]["missed"] and thu[1]["recorded"], thu
    assert thu[1]["name"].endswith("Activity-Based-Costing"), thu[1]
results.append(run("the week grid marks each class recorded, missed, or upcoming", t6))


def t7():
    # Thursday 13:30: ACCT-4321 at 14:00 is within the 45 minute tolerance,
    # so it is live; ENTR-3306 at 12:00 ended at 13:30 but has an hour of
    # grace before it counts as missed.
    out = insights.compute(ROWS, SCHEDULE, datetime(2026, 9, 10, 13, 30))
    thu = {c["course"]: c for d in out["week"]["days"] if d["day"] == "Thu" for c in d["classes"]}
    assert thu["ACCT-4321"]["now"], thu["ACCT-4321"]
    assert not thu["ENTR-3306"]["missed"] and not thu["ENTR-3306"]["now"], thu["ENTR-3306"]
    assert any(d["today"] for d in out["week"]["days"] if d["day"] == "Thu")
    fri = {c["course"]: c for d in out["week"]["days"] if d["day"] == "Fri" for c in d["classes"]}
    assert not fri["RELI-3304"]["missed"] and not fri["RELI-3304"]["recorded"], "Friday is still upcoming"
    # 14:35 the same day: ENTR-3306's grace is up; ACCT-4321 is still in session.
    later = insights.compute(ROWS, SCHEDULE, datetime(2026, 9, 10, 14, 35))
    thu = {c["course"]: c for d in later["week"]["days"] if d["day"] == "Thu" for c in d["classes"]}
    assert thu["ENTR-3306"]["missed"], thu["ENTR-3306"]
    assert thu["ACCT-4321"]["now"], thu["ACCT-4321"]
results.append(run("a class is live from the tolerance before it starts and missed only after the grace", t7))


def t8():
    t = OUT["totals"]
    assert t["lectures"] == 7 and t["failures"] == 1, t
    assert t["courses"] == 4, t
    assert t["first"] == "2026-08-31", t
    assert t["minutes"] == round(8700 / 60) and t["words"] == 18900 and t["actions"] == 4, t
    # This week: four lectures filed (one UNKNOWN), all eight classes due by Saturday,
    # three of them covered.
    assert t["this_week"] == 4, t
    assert t["scheduled_this_week"] == 8 and t["due_this_week"] == 8, t
    assert t["covered_this_week"] == 3, t
    # Two weeks of eight meetings each are due; 3 + 3 covered.
    assert t["due"] == 16 and t["covered"] == 6, t
    # Friday's two classes were missed, so no streak is running.
    assert t["streak"] == 0, t
results.append(run("the totals count lectures, coverage, and this week", t8))


def t9():
    # Thursday evening: the streak runs back from Thu 14:00 ACCT (recorded)
    # to Thu 12:00 ENTR-3306 (missed), so it is one class long.
    out = insights.compute(ROWS, SCHEDULE, datetime(2026, 9, 10, 20, 0))
    assert out["totals"]["streak"] == 1, out["totals"]
    # Tuesday evening: Tue 14:00 and Tue 12:00 recorded, Mon 9:00 missed.
    out = insights.compute(ROWS, SCHEDULE, datetime(2026, 9, 8, 20, 0))
    assert out["totals"]["streak"] == 2, out["totals"]
    assert out["totals"]["due_this_week"] == 3 and out["totals"]["covered_this_week"] == 2, out["totals"]
results.append(run("the streak counts consecutive covered classes back from the last one due", t9))


# Classes that did not meet -------------------------------------------------
# Friday Sep 11 held ENTR-4306 at 9 and RELI-3304 at 12. Both went
# unrecorded in the log above, which is what drops the streak to zero.
FRIDAY_OFF = {("ENTR-4306", "2026-09-11"), ("RELI-3304", "2026-09-11")}


def t9b():
    off = insights.compute(ROWS, SCHEDULE, NOW, canceled=FRIDAY_OFF)
    t = off["totals"]
    # The streak now runs back from Thursday: Thu 14:00 ACCT was recorded,
    # Thu 12:00 ENTR-3306 was not. Friday is skipped, not counted as a miss.
    assert t["streak"] == 1, t
    # Two weeks of eight, less the two that did not meet.
    assert t["due"] == 14 and t["covered"] == 6, t
    assert t["canceled"] == 2, t
    assert t["due_this_week"] == 6 and t["scheduled_this_week"] == 6, t
    assert t["covered_this_week"] == 3, "excusing a class must not change what was recorded"
    # The week's target drops with it, so the chart does not keep drawing a
    # bar the student can no longer reach.
    current = next(w for w in off["weeks"] if w["current"])
    assert current["scheduled"] == 6, current
    before = next(w for w in off["weeks"] if w["start"] == "2026-08-31")
    assert before["scheduled"] == 8, "an earlier week is untouched"
results.append(run("a class that did not meet leaves the streak and the target alone", t9b))


def t9c():
    off = insights.compute(ROWS, SCHEDULE, NOW, canceled=FRIDAY_OFF)
    friday = next(d for d in off["week"]["days"] if d["date"] == "2026-09-11")
    for c in friday["classes"]:
        assert c["canceled"] is True, c
        assert c["missed"] is False, "a class that did not meet is not a missed one"
    thursday = next(d for d in off["week"]["days"] if d["date"] == "2026-09-10")
    assert any(c["missed"] for c in thursday["classes"]), "a real miss still reads as one"
    assert not any(c["canceled"] for c in thursday["classes"]), thursday
results.append(run("the week grid marks a class that did not meet instead of missing it", t9c))


def t9d():
    # The recording is the answer to whether the class met, so a lecture
    # filed against a canceled meeting counts for the student anyway.
    off = insights.compute(ROWS, SCHEDULE, NOW,
                           canceled={("ACCT-4321", "2026-09-10")})
    t = off["totals"]
    assert t["due"] == 16 and t["covered"] == 6, t
    assert t["canceled"] == 0, t
    slot = next(c for d in off["week"]["days"] if d["date"] == "2026-09-10"
                for c in d["classes"] if c["course"] == "ACCT-4321")
    assert slot["recorded"] is True and slot["canceled"] is False, slot
results.append(run("a lecture filed against a canceled class still counts", t9d))


def t9e():
    # The notes come back on the slots they belong to, for the chip's tooltip.
    off = insights.compute(ROWS, SCHEDULE, NOW,
                           canceled={("RELI-3304", "2026-09-11"): "campus closed"})
    slot = next(c for d in off["week"]["days"] if d["date"] == "2026-09-11"
                for c in d["classes"] if c["course"] == "RELI-3304")
    assert slot["canceled"] is True and slot["note"] == "campus closed", slot
    # Nothing canceled at all is the same answer as before the feature.
    assert insights.compute(ROWS, SCHEDULE, NOW, canceled=set()) == OUT
results.append(run("a cancellation carries its reason, and none changes nothing", t9e))


def t10():
    empty = insights.compute([], SCHEDULE, NOW)
    assert empty["totals"]["lectures"] == 0 and empty["totals"]["streak"] == 0, empty["totals"]
    assert empty["totals"]["due"] == 0, "nothing is due before the first lecture is ever filed"
    assert all(w["scheduled"] == 0 for w in empty["weeks"]), empty["weeks"]
    assert [c["count"] for c in empty["courses"]] == [0, 0, 0, 0], empty["courses"]
    assert len(empty["week"]["days"]) == 5, empty["week"]

    no_schedule = insights.compute(ROWS, None, NOW)
    assert no_schedule["week"]["days"] == [], no_schedule["week"]
    assert no_schedule["totals"]["lectures"] == 7 and no_schedule["totals"]["scheduled_this_week"] == 0
    codes = [c["code"] for c in no_schedule["courses"]]
    assert codes == ["ACCT-4321", "ENTR-3306", "ENTR-4306", "UNKNOWN"], codes
results.append(run("an empty log or a missing schedule yields zeros, not errors", t10))


def t11():
    from intake import gui
    home = config.HOME_DIR
    (home / "pipeline.log").write_text(LOG)
    config.LOG_FILE = home / "pipeline.log"
    client = gui.app.test_client()
    body = client.get("/api/status").get_json()
    ins = body["insights"]
    assert ins["totals"]["lectures"] == 7, ins["totals"]
    assert body["recent"][0]["name"].endswith("Untitled"), body["recent"][0]
    assert body["recent"][1]["seconds"] == 4200, "recent rows must carry the measurements"
    html = client.get("/").get_data(as_text=True)
    for piece in ("Lectures per week", "Recent lectures", "This week", "Study assistant", "Pipeline",
                  "Coming in v3", 'id="tiles"', 'id="weeksChart"'):
        assert piece in html, f"the page is missing {piece!r}"
    assert 'disabled aria-label="Ask the study assistant' in html, "the assistant box must be inert"
results.append(run("the status payload carries insights and the page has a place for each", t11))


def t12():
    # The whole point of t3b, at the level the user actually feels it: one
    # unusable number in the log must not be able to stop the panel.
    from intake import gui
    home = config.HOME_DIR
    config.LOG_FILE = home / "pipeline.log"
    config.LOG_FILE.write_text(
        LOG + "2026-09-15T10:00:00\tACCT-4321\tlecture.m4a\tACCT-4321_2026-09-15_Topic"
        '\thttps://drive.test/x\t\t{"seconds":1e999,"words":12}\n')
    client = gui.app.test_client()
    res = client.get("/api/status")
    assert res.status_code == 200, f"one bad measurement returned {res.status_code}"
    body = res.get_json()
    for key in ("recording", "watcher", "inbox", "insights", "recent", "configured"):
        assert key in body, f"/api/status lost {key}"
    # The line is still listed, just without the measurement nobody can use.
    row = next(r for r in body["recent"] if r["name"].endswith("Topic"))
    assert row.get("seconds") in (None, 0), row
results.append(run("one unusable measurement does not take the panel down", t12))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
