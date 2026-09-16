#!/usr/bin/env python3
"""Fail when this repo's lecture prompt has drifted from syllabus-accounts'.

The mirror of syllabus-accounts/.github/workflows/prompt_parity.py. The summary
prompt exists twice: here in intake/schemas.py, which a Mac on its own keys
runs, and there in src/prompts.ts, which every managed account runs. They have
drifted twice, and the second time shipped older action-item rules to the
managed path until a benchmark showed different output.

Both sides are needed, and they are not redundant. Each repo's check only runs
when THAT repo is pushed, so the syllabus-accounts copy cannot see a reword
that happens here. This one covers exactly the event that one structurally
misses.

Two differences from the syllabus-accounts copy, both bugs found while writing
it: each file is sliced to its LECTURE prompt before matching, because two of
the anchors also appear in the call prompt further down and the original only
worked by accident of ordering; and the rules are reported by name rather than
by position.

    python3 prompt_parity.py <schemas.py> <prompts.ts>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ANCHORS = [
    "- An action item is something the instructor assigned",
    "- Capture every assignment that clears that bar",
    "- Do not invent action items, and do not invent deadlines",
    "- Keep each action item's task to the errand alone",
    "- For each action item, resolve any relative deadline",
]

# Where each file's LECTURE prompt starts and ends. Slicing first is what
# keeps an anchor that also appears in the call prompt from matching there.
LECTURE_SPANS = {
    "schemas.py": ("LECTURE_SYSTEM_PROMPT = ", '"""'),
    "prompts.ts": ("const LECTURE_SYSTEM = ", "`;"),
}


def lecture_prompt(text: str, kind: str, source: str) -> str:
    start_marker, end_marker = LECTURE_SPANS[kind]
    start = text.find(start_marker)
    if start < 0:
        sys.exit(f"{source}: no {start_marker!r} in this file. Did it move or get renamed?")
    body = text[start + len(start_marker):]
    end = body.find(end_marker, 1)
    return body if end < 0 else body[:end]


def normalize(text: str) -> str:
    """One line, single spaced, with either language's continuations removed."""
    return re.sub(r"\s+", " ", text.replace("\\\n", "").replace("\\", "")).strip()


def rule(prompt: str, anchor: str, source: str) -> str:
    start = prompt.find(anchor)
    if start < 0:
        sys.exit(f"{source}: cannot find the rule starting {anchor!r}.\n"
                 f"If it was deliberately reworded, update ANCHORS in BOTH repos' "
                 f"copies of this script in the same change.")
    rest = prompt[start + len(anchor):]
    ends = [m.start() for m in re.finditer(r"\n-\s", rest)]
    return normalize(anchor + (rest[: ends[0]] if ends else rest))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.exit("usage: prompt_parity.py <schemas.py> <prompts.ts>")
    here = lecture_prompt(Path(argv[1]).read_text(), "schemas.py", "intake/schemas.py")
    there = lecture_prompt(Path(argv[2]).read_text(), "prompts.ts", "syllabus-accounts/src/prompts.ts")

    drifted = []
    for anchor in ANCHORS:
        a = rule(here, anchor, "intake/schemas.py")
        b = rule(there, anchor, "syllabus-accounts/src/prompts.ts")
        print(("match   " if a == b else "DRIFTED ") + anchor[2:62] + "...")
        if a != b:
            drifted.append((anchor, a, b))

    if drifted:
        print("\nThe lecture prompt differs between the two repos. A Mac on its own keys")
        print("and a managed account would summarize the same lecture by different rules.\n")
        for anchor, a, b in drifted:
            print(f"--- {anchor}")
            print(f"  this repo (intake/schemas.py): {a}")
            print(f"  syllabus-accounts (prompts.ts): {b}\n")
        return 1

    print(f"\nall {len(ANCHORS)} action-item rules match syllabus-accounts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
