"""Tests for tasktext.py: cleaning a task, and telling two of them apart.

Pure string work, so this needs no token, no database and no network. From the
project root:

    .venv/bin/python test_tasktext.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from intake import tasktext as tt  # noqa: E402


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
    # Notion shows rich_text verbatim, so anything left here reaches the user.
    for raw, want in (
        ("**Read** chapter 7", "Read chapter 7"),
        ("- Submit the case memo", "Submit the case memo"),
        ("1. Submit the case memo", "Submit the case memo"),
        ("Read `chapter 7`", "Read chapter 7"),
        ("Read ~~chapter 6~~ chapter 7", "Read chapter 6 chapter 7"),
        ("[Read chapter 7](https://x.test/ch7)", "Read chapter 7"),
        ("Read chapter 7\nand chapter 8", "Read chapter 7 and chapter 8"),
    ):
        assert tt.clean(raw) == want, f"{raw!r} became {tt.clean(raw)!r}"
results.append(run("markdown and line breaks never reach the checkbox", t1))


def t2():
    for raw, want in (
        ("Please read chapter 7", "Read chapter 7"),
        ("Make sure to read chapter 7", "Read chapter 7"),
        ("Please make sure to read chapter 7", "Read chapter 7"),
        ("Don't forget to submit the memo", "Submit the memo"),
        ("You need to finish the problem set", "Finish the problem set"),
    ):
        assert tt.clean(raw) == want, f"{raw!r} became {tt.clean(raw)!r}"
results.append(run("filler openers are stripped, even when they stack", t2))


def t3():
    # The due date is already a column on the page and a property on a row.
    for raw, want in (
        ("Read chapter 7 before class", "Read chapter 7"),
        ("Read chapter 7 by Thursday", "Read chapter 7"),
        ("Submit the case memo before the exam", "Submit the case memo"),
        ("Finish the problem set by Oct 3", "Finish the problem set"),
    ):
        assert tt.clean(raw) == want, f"{raw!r} became {tt.clean(raw)!r}"
results.append(run("a deadline restated in the task is dropped", t3))


def t4():
    # Stripping the tail off these would leave a verb with no object.
    assert tt.clean("Study for the final") == "Study for the final"
    assert tt.clean("Prepare for Monday") == "Prepare for Monday"
    # And an open vocabulary after the preposition would eat a real object.
    assert tt.clean("Submit the memo on Canvas") == "Submit the memo on Canvas"
results.append(run("a tail that carries the task is left alone", t4))


def t5():
    long_task = ("Read chapter 7 of the textbook and come prepared to "
                 "discuss the three approaches to overhead allocation that "
                 "the chapter lays out in its second half")
    out = tt.clean(long_task)
    assert len(out) <= tt.MAX_LENGTH + 1, len(out)
    assert out.endswith("…"), out
    # Cut between words, not through one.
    assert not out[:-1].endswith(" "), out
    assert long_task.startswith(out[:-1]), out
results.append(run("an overlong task is cut on a word boundary", t5))


def t6():
    out = tt.clean("Read chapter 7. This will be on the midterm.")
    assert out == "Read chapter 7", out
results.append(run("rationale after the errand is dropped", t6))


def t7():
    # The case this whole module exists for: one assignment, three lectures.
    same = [
        ("Read chapter 7", "Please read Chapter Seven"),
        ("Read chapter 7 before class", "Read ch. 7"),
        ("Read chapter 7", "Chapter 7 reading"),
        ("Submit the case memo", "Submit case memo"),
        ("Finish problem set 3", "Complete problem set 3"),
    ]
    for left, right in same:
        assert tt.same(left, right), f"{left!r} should match {right!r}"
results.append(run("the same assignment reworded is recognized", t7))


def t8():
    # "Read chapter 7" and "Read chapter 8" are 92% identical as text.
    different = [
        ("Read chapter 7", "Read chapter 8"),
        ("Read chapter 7", "Read chapters 7 and 8"),
        ("Problem set 3", "Problem set 4"),
        ("Read chapter 7", "Submit the case memo"),
        ("Read chapter 7", ""),
    ]
    for left, right in different:
        assert not tt.same(left, right), f"{left!r} wrongly matched {right!r}"
results.append(run("a number that differs means a different task", t8))


def t9():
    assert tt.strip_course_prefix("ACCT-4321: Read chapter 7") == "Read chapter 7"
    assert tt.strip_course_prefix("ACCT 4321: Read chapter 7") == "Read chapter 7"
    assert tt.strip_course_prefix("Read chapter 7") == "Read chapter 7"
results.append(run("a course prefix comes off before comparing", t9))


def t10():
    assert tt.clean("") == ""
    assert tt.clean("   ") == ""
    assert tt.clean("**") == ""
    assert tt.key("") == ""
results.append(run("empty and markup-only input degrade quietly", t10))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
