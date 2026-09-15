"""Turning what the model said about a task into something Notion can show.

Two jobs, both on the task string itself.

`clean` makes one task short and literal. The model writes prose: markdown
emphasis, a polite opener, the deadline restated at the end. Notion's
rich_text is a plain string with no markdown parsing, so every asterisk,
bullet and newline survives into the checkbox exactly as written.

`same` decides whether two tasks are the same errand. An instructor who
mentions one assignment across three class periods gets summarized three
times and never in the same words, so comparing the strings as written files
the same reading three times over.

Stdlib only, and no Notion knowledge, so both can be tested on their own.
"""

from __future__ import annotations

import difflib
import re

# How close two normalized tasks have to be to count as one errand. Set by
# trying it against real restatements: 0.8 merged "chapter 7" with "chapter 8"
# before the digit guard existed, 0.9 missed most rephrasings.
RATIO = 0.85

# The share of words two tasks must have in common when their word order
# differs ("Read chapter 7" against "Chapter 7 reading"), which the sequence
# ratio alone scores too low.
OVERLAP = 0.7

# Longest checkbox text worth writing. A complying model lands near 60
# characters; this only bites when it returns a paragraph, and the link to the
# notes is right there for whatever gets cut.
MAX_LENGTH = 140

# Markdown that arrives as literal characters: emphasis, code, strikethrough.
_EMPHASIS_RE = re.compile(r"\*\*|__|~~|[*_`]")

# "[Read chapter 7](https://...)" keeps its label and loses the target.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")

# A leading bullet, number or heading marker from a list the model wrote.
_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)]|#{1,6})\s+")

# Openers that say nothing. Stripped repeatedly, since they stack:
# "Please make sure to read chapter 7".
_OPENER_RE = re.compile(
    r"^(?:please|kindly|make\s+sure\s+(?:to|you|that\s+you)|be\s+sure\s+to|"
    r"don'?t\s+forget\s+to|remember\s+to|try\s+to|go\s+ahead\s+and|"
    r"students?\s+(?:should|must|need\s+to)|"
    r"you(?:'ll)?\s+(?:should|must|need\s+to|will\s+need\s+to|have\s+to))\s+",
    re.I,
)

# A trailing restatement of the deadline. The due date is already a column on
# the weekly page and a property on a database row, so repeating it in the
# checkbox costs a line and says nothing.
#
# The vocabulary after the preposition is deliberately a closed list: an open
# one turned "Submit the memo on Canvas" into "Submit the memo".
_WHEN_TAIL_RE = re.compile(
    r"""[\s,;:–—-]*\b
    (?:by|before|due|prior\s+to|in\s+time\s+for|ahead\s+of|for|on|no\s+later\s+than)\s+
    (?:the\s+|our\s+|next\s+|this\s+|coming\s+|our\s+next\s+)*
    (?:class|lecture|session|exam|test|quiz|midterm|final|deadline|then|
       today|tomorrow|tonight|week|weekend|
       monday|tuesday|wednesday|thursday|thursdays|friday|saturday|sunday|
       mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun|
       [A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?|\d{1,2}/\d{1,2})
    (?:'s)?
    (?:\s+(?:class|lecture|session|period|meeting))?
    \s*[.!]?$""",
    re.I | re.X,
)

# Stripping the deadline off "Study for the final" leaves "Study", which is
# not a task. Below this many words the tail is left alone.
_MIN_WORDS_AFTER_TAIL = 2

# A course code the weekly page puts in front of the task, so an existing
# checkbox can be compared against a freshly generated task.
_COURSE_PREFIX_RE = re.compile(r"^\s*[A-Za-z]{2,6}[-\s]?\d{2,4}[A-Za-z]?\s*:\s*")

# Words that carry no identity. Dropped before two tasks are compared so
# "Read the chapter on costing" and "Read chapter on costing" match.
_STOPWORDS = frozenset(
    "a an the of in on at to for from your our their this that these those "
    "and or all any some is are be will please about over up".split()
)

# Spellings of the same thing, normalized so only one reaches the comparison.
_SYNONYMS = {
    "chapter": "ch", "chapters": "ch", "chap": "ch", "chaps": "ch", "ch": "ch",
    "page": "p", "pages": "p", "pp": "p", "pg": "p",
    "problem": "prob", "problems": "prob", "pset": "pset",
    "homework": "hw", "hw": "hw", "assignment": "hw", "assignments": "hw",
    "question": "q", "questions": "q", "exercise": "q", "exercises": "q",
    "read": "read", "reading": "read", "reads": "read",
    "write": "write", "writing": "write", "written": "write",
    "submit": "submit", "submission": "submit", "turn": "submit",
    "review": "review", "reviewing": "review",
    "study": "study", "studying": "study",
    "finish": "finish", "finishing": "finish", "complete": "finish",
    "completing": "finish", "prepare": "prepare", "preparing": "prepare",
}

_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12",
}

_DIGITS_RE = re.compile(r"\d+")


def strip_markdown(text: str) -> str:
    """Drop markup Notion would render as literal characters."""
    text = _LINK_RE.sub(r"\1", text or "")
    text = _MARKER_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text)
    # A newline inside a to_do breaks the checkbox across lines.
    return " ".join(text.split())


def shorten(text: str, limit: int = MAX_LENGTH) -> str:
    """Cut to `limit` on a word boundary rather than mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[:limit]) + "…"


def clean(task: str, limit: int = MAX_LENGTH) -> str:
    """One checkbox's worth of text: the errand, and nothing around it."""
    text = strip_markdown(task)
    if not text:
        return ""

    # The errand is the first sentence; what follows is rationale ("This will
    # be on the exam") that belongs in the detail, not the checkbox.
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text, maxsplit=1)[0].strip()
    if first:
        text = first

    previous = None
    while text and text != previous:
        previous = text
        text = _OPENER_RE.sub("", text).strip()

    stripped = _WHEN_TAIL_RE.sub("", text).strip(" ,;:-")
    if len(stripped.split()) >= _MIN_WORDS_AFTER_TAIL:
        text = stripped

    text = text.rstrip(" .,;:")
    if text:
        text = text[0].upper() + text[1:]
    return shorten(text, limit)


def strip_course_prefix(text: str) -> str:
    """Remove a leading course code, which the weekly checkbox prepends."""
    return _COURSE_PREFIX_RE.sub("", text or "").strip()


def key(task: str) -> str:
    """A task reduced to what identifies it, for comparing against another.

    Wording, punctuation, filler and the usual abbreviations all come out, so
    "Please read Chapter 7" and "Read ch. 7 of the text" land on the same
    string. Nothing here is meant to be shown to anyone.
    """
    text = strip_course_prefix(clean(task, limit=10_000)).lower()
    words = []
    for word in re.findall(r"[a-z0-9]+", text):
        word = _NUMBER_WORDS.get(word, word)
        word = _SYNONYMS.get(word, word)
        if word in _STOPWORDS:
            continue
        # A crude plural strip. It mangles words ("class" to "clas") but does
        # so identically on both sides of a comparison, which is all it needs.
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.append(word)
    return " ".join(words)


def same(left: str, right: str) -> bool:
    """Whether two tasks are the same errand said two different ways.

    Numbers are matched exactly rather than fuzzily: "Read chapter 7" and
    "Read chapter 8" are 92% identical as text and are not the same homework.
    """
    a, b = key(left), key(right)
    if not a or not b:
        return False
    if a == b:
        return True

    left_digits = set(_DIGITS_RE.findall(a))
    right_digits = set(_DIGITS_RE.findall(b))
    if (left_digits or right_digits) and left_digits != right_digits:
        return False

    if difflib.SequenceMatcher(None, a, b).ratio() >= RATIO:
        return True

    # Word order differs often enough to be worth a second look: "Read
    # chapter 7" against "Chapter 7 reading" scores poorly in sequence but
    # shares every word.
    left_words, right_words = set(a.split()), set(b.split())
    overlap = len(left_words & right_words) / max(
        len(left_words), len(right_words)
    )
    return overlap >= OVERLAP
