#!/usr/bin/env python3
"""Enforce the house copy rules on text a customer actually reads.

The rules (CLAUDE.md): no em dashes, US spellings only, US phrasing. They
apply to client-facing text: page copy, labels, meta descriptions, alt text,
strings in data files. They explicitly do NOT apply to code comments, and
quoted reviews and carrier-registered SMS text are exempt even when they break
a rule.

Why this is not a grep. Across this repo a naive `grep -c "—"` flags 12 lines
of which 9 are out of scope: docstrings, code comments, and two regular
expressions where the em dash is a character in a class ([-–—]) and
"correcting" it would silently break date parsing. A 77% false-positive rate
is how a check gets disabled instead of obeyed.

So Python is read through `tokenize`: comments are skipped as tokens rather
than guessed at, docstrings are skipped as triple-quoted literals, and any
r-prefixed literal is skipped because it is a pattern and not prose. HTML has
its comments and CSS block comments stripped before scanning, which is what
keeps index.html's "colour" (inside a CSS comment) from failing the build.

    python3 copy_standards.py            # scan the default paths
    python3 copy_standards.py a.py b.html
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

# What a customer reads: the panel's pages, the strings the pipeline writes
# into their notes, and the release notes shown on the download page. README
# is for developers and is deliberately not included.
DEFAULT_PATHS = ["intake", "packaging/release-notes.md"]
SUFFIXES = {".py", ".html", ".md"}

EM_DASH = "—"

BRITISH_SPELLINGS = [
    "organised", "organisation", "licence", "honoured", "colour", "favourite",
    "centrepiece", "cancelled", "cancelling", "behaviour", "catalogue",
    "analyse", "analysed", "optimise", "optimised", "recognise", "recognised",
    "apologise", "customise", "personalised", "summarise", "summarised",
    "travelled", "labelled", "modelling", "defence", "offence", "practise",
    "programme", "grey", "enrolment", "fulfil", "speciality",
]

# Two correctly spelled words can still read as British. The rules name these.
BRITISH_PHRASES = [
    "straight away", "different to", "car park", "post code", "postcode",
    "at the weekend", "soakaway", "in hospital", "maths", "whilst", "amongst",
    "sort out the", "ring us", "book an appointment with",
]


def failures_in(text: str, rule_name: str, needles: list[str]) -> list[tuple[str, str]]:
    out = []
    low = text.lower()
    for needle in needles:
        if re.search(rf"\b{re.escape(needle)}\b", low):
            out.append((rule_name, needle))
    return out


def check_text(text: str) -> list[tuple[str, str]]:
    """Every rule this fragment breaks."""
    found = []
    if EM_DASH in text:
        found.append(("em dash", EM_DASH))
    found += failures_in(text, "British spelling", BRITISH_SPELLINGS)
    found += failures_in(text, "British phrasing", BRITISH_PHRASES)
    return found


def python_strings(source: str):
    """Every string literal a reader could see, as (line, text).

    Skips comments (not client-facing by rule), triple-quoted literals
    (docstrings), and r-prefixed literals (patterns, not prose). Handles both
    the pre-3.12 single STRING token for f-strings and the 3.12+ split into
    FSTRING_START / FSTRING_MIDDLE / FSTRING_END.
    """
    skip_fstring = False
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise RuntimeError(f"could not tokenize: {exc}") from exc

    for tok in tokens:
        name = tokenize.tok_name[tok.type]
        if name == "COMMENT":
            continue
        if name == "FSTRING_START":
            prefix = tok.string.rstrip("\"'").lower()
            skip_fstring = "r" in prefix or tok.string.endswith(('"""', "'''"))
            continue
        if name == "FSTRING_END":
            skip_fstring = False
            continue
        if name == "FSTRING_MIDDLE":
            if not skip_fstring:
                yield tok.start[0], tok.string
            continue
        if name == "STRING":
            prefix = re.match(r"[A-Za-z]*", tok.string).group(0).lower()
            if "r" in prefix:
                continue
            body = tok.string[len(prefix):]
            if body.startswith(('"""', "'''")):
                continue          # a docstring, not copy
            yield tok.start[0], body


HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def scan(path: Path) -> list[str]:
    """Human-readable problems in one file."""
    text = path.read_text(encoding="utf-8")
    problems = []

    if path.suffix == ".py":
        try:
            for line, fragment in python_strings(text):
                for rule, needle in check_text(fragment):
                    problems.append(f"{path}:{line}: {rule} in a user-facing string: {needle!r}")
        except RuntimeError as exc:
            problems.append(f"{path}: {exc}")
        return problems

    if path.suffix == ".html":
        # Comments go first, so a rule broken inside one is not a failure.
        stripped = BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"),
                                     HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text))
    else:
        stripped = text

    for i, line in enumerate(stripped.splitlines(), start=1):
        for rule, needle in check_text(line):
            problems.append(f"{path}:{i}: {rule}: {needle!r}")
    return problems


def files_under(paths: list[str]):
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for child in sorted(p.rglob("*")):
                if child.suffix in SUFFIXES and "__pycache__" not in child.parts:
                    yield child
        elif p.exists() and p.suffix in SUFFIXES:
            yield p


def main(argv: list[str]) -> int:
    targets = argv[1:] or DEFAULT_PATHS
    problems, scanned = [], 0
    for path in files_under(targets):
        scanned += 1
        problems.extend(scan(path))

    if problems:
        print(f"{len(problems)} copy-standard violation(s) in {scanned} files:\n")
        for line in problems:
            print("  " + line)
        print("\nHouse rules (CLAUDE.md): no em dashes, US spellings, US phrasing.")
        print("Use a period, colon, comma, semicolon, or a middot for label-value pairs.")
        print("Code comments, docstrings and r-prefixed patterns are out of scope and not flagged.")
        return 1

    print(f"{scanned} files scanned, no copy-standard violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
