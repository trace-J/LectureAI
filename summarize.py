"""Transcript -> study summary, key terms, and action items, via Claude.

One API call per lecture. The model returns JSON; everything downstream
(filenames, the uploaded .md) is built from that plus the schedule lookup.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

import config

MAX_SLUG_WORDS = 4
FALLBACK_SLUG = "Lecture-Notes"


class KeyTerm(BaseModel):
    term: str = Field(description="The term as the instructor used it.")
    definition: str = Field(description="One line, in plain language.")


class ActionItem(BaseModel):
    """One thing the student has to do, with a date if the lecture gave one.

    Every field is a plain string rather than a date or an enum. The schema is
    enforced server-side, and a nullable or enum-typed field is a place for
    that enforcement to reject a response the pipeline could otherwise have
    used. An empty string is unambiguous and normalizing in Python is free.
    """

    task: str = Field(
        description=(
            "What the student has to do, phrased as an instruction to "
            "themselves: 'Read chapter 7', 'Submit the case memo'. Do not "
            "include the due date here."
        )
    )
    due_date: str = Field(
        description=(
            "The due date as YYYY-MM-DD. Resolve anything relative against "
            "the lecture date given above, so 'next Thursday' becomes a real "
            "date. Use an empty string if the instructor gave no deadline at "
            "all. Never guess a date that was not stated or implied."
        )
    )
    kind: str = Field(
        description=(
            "One of: assignment, reading, quiz, exam, project, other."
        )
    )


class LectureSummary(BaseModel):
    """Schema the model's response is constrained to.

    Enforced server-side, which is the point: asking for JSON in the prompt and
    parsing it ourselves failed intermittently when the model emitted a literal
    newline inside a string, making the whole object unparseable.
    """

    summary_md: str = Field(
        description=(
            "The study summary as GitHub-flavored markdown. Use ## headings for "
            "major topics with prose paragraphs under them, explaining concepts "
            "in full sentences. Use lists only for genuinely enumerable things "
            "like the steps of a procedure."
        )
    )
    topic_slug: str = Field(
        description=(
            "2 to 4 words naming what this lecture was actually about, in "
            "Title-Case-With-Hyphens, e.g. Job-Order-Costing or "
            "Statement-Of-Cash-Flows. Name the specific topic, never the course "
            "and never the word Lecture."
        )
    )
    key_terms: list[KeyTerm] = Field(
        description="Terms a student would need defined to follow the lecture."
    )
    action_items: list[ActionItem] = Field(
        description=(
            "Any assignment, reading, quiz, exam, or deadline mentioned. "
            "Empty list if none were mentioned. Never invent one."
        )
    )

SYSTEM_PROMPT = """You summarize university lecture transcripts for a student \
who attended the class and is studying from your notes later.

The transcript comes from automatic speech recognition. It has no speaker \
labels, no punctuation guarantees, and will contain misheard words, false \
starts, roll call, and administrative chatter. Work past all of that and \
focus on the academic content.

Write for someone reviewing before an exam:

- Explain the main concepts, don't just list them. If the instructor worked \
through an example or a calculation, walk through the reasoning and keep the \
numbers. If they explained *why* something works, capture that explanation.
- Preserve the instructor's emphasis. Anything they repeated, said would be \
on the exam, or flagged as commonly misunderstood deserves prominence.
- Skip attendance, scheduling chatter, and technical difficulties unless they \
carry a deadline or a requirement.
- Do not invent action items. If the instructor never mentioned a deadline, \
return an empty list.
- For each action item, resolve any relative deadline against the lecture date \
you are given: "next Thursday", "a week from today" and "before the exam" all \
become a real YYYY-MM-DD. If the instructor genuinely set no deadline, leave \
the date empty rather than inventing one."""

USER_TEMPLATE = """Course: {course}
Date: {date}

Lecture transcript:

{transcript}"""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` wrappers the model may add despite instructions."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def slugify_topic(raw: str) -> str:
    """Force a topic into Title-Case-With-Hyphens, at most MAX_SLUG_WORDS."""
    words = re.findall(r"[A-Za-z0-9]+", raw or "")
    if not words:
        return FALLBACK_SLUG
    return "-".join(w.capitalize() if not w.isupper() else w
                    for w in words[:MAX_SLUG_WORDS])


def build_filename(course: str, date: str, topic_slug: str) -> str:
    """{COURSE}_{YYYY-MM-DD}_{Topic-Slug} — course comes from the schedule."""
    return f"{course}_{date}_{slugify_topic(topic_slug)}"


ACTION_KINDS = {"assignment", "reading", "quiz", "exam", "project", "other"}


def _clean_date(raw: str) -> str:
    """A real YYYY-MM-DD, or empty. Guards against a plausible-looking date."""
    text = str(raw or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def normalize_actions(raw_items, course: str, date: str) -> list[dict]:
    """Clean up action items and give the undated ones a due date.

    An item the instructor never dated is dated to the next class meeting,
    since that is when a reading or problem set is usually wanted. `date_source`
    records which happened, so an assumption is never presented as something
    the instructor said.
    """
    items: list[dict] = []
    for raw in raw_items or []:
        if isinstance(raw, str):
            entry = {"task": raw, "due_date": "", "kind": "other"}
        elif isinstance(raw, dict):
            entry = raw
        else:  # a pydantic ActionItem
            entry = {
                "task": getattr(raw, "task", ""),
                "due_date": getattr(raw, "due_date", ""),
                "kind": getattr(raw, "kind", "other"),
            }

        task = str(entry.get("task", "")).strip()
        if not task:
            continue

        kind = str(entry.get("kind", "other")).strip().lower()
        if kind not in ACTION_KINDS:
            kind = "other"

        due = _clean_date(entry.get("due_date", ""))
        if due:
            source = "stated"
        else:
            due = config.next_class_meeting(course, date) or ""
            source = "assumed" if due else "none"

        items.append({"task": task, "due_date": due, "kind": kind,
                      "date_source": source})
    return items


def _save_failed_response(raw: str, course: str, date: str) -> None:
    """Keep the raw response when parsing fails, so it can be diagnosed."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.WORK_DIR / f"summarize-failure_{course}_{date}_{stamp}.txt"
    try:
        path.write_text(raw)
        log(f"  raw response saved to {path}")
    except OSError:
        pass


def _parse(raw: str) -> dict:
    """Parse the model's JSON, degrading gracefully rather than raising."""
    try:
        data = json.loads(_strip_fences(raw))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        log(f"  WARNING: could not parse JSON ({exc}); keeping raw text as summary")
        return {
            "summary_md": raw.strip(),
            "topic_slug": FALLBACK_SLUG,
            "key_terms": [],
            "action_items": [],
        }

    terms = []
    for item in data.get("key_terms") or []:
        if isinstance(item, dict) and item.get("term"):
            terms.append({
                "term": str(item["term"]).strip(),
                "definition": str(item.get("definition", "")).strip(),
            })
        elif isinstance(item, str) and item.strip():
            terms.append({"term": item.strip(), "definition": ""})

    return {
        "summary_md": str(data.get("summary_md", "")).strip(),
        "topic_slug": slugify_topic(str(data.get("topic_slug", ""))),
        "key_terms": terms,
        # Left raw here; summarize() normalizes once it knows course and date.
        "action_items": data.get("action_items") or [],
    }


def summarize(transcript: str, course: str, date: str) -> dict:
    """Summarize a transcript. Returns summary_md, topic_slug, key_terms, action_items."""
    if not transcript.strip():
        raise ValueError("transcript is empty")

    client = anthropic.Anthropic(api_key=config.require("ANTHROPIC_API_KEY"))
    log(f"summarizing {len(transcript.split())} words for {course} on {date} "
        f"via {config.CLAUDE_MODEL}")

    response = client.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": USER_TEMPLATE.format(
                course=course, date=date, transcript=transcript.strip()
            ),
        }],
        output_format=LectureSummary,
    )

    parsed = getattr(response, "parsed_output", None)
    if parsed is not None:
        result = {
            "summary_md": parsed.summary_md.strip(),
            "topic_slug": slugify_topic(parsed.topic_slug),
            "key_terms": [
                {"term": t.term.strip(), "definition": t.definition.strip()}
                for t in parsed.key_terms if t.term.strip()
            ],
            "action_items": normalize_actions(parsed.action_items, course, date),
        }
    else:
        # Shouldn't happen with a schema, but a truncated or refused response
        # still needs to degrade rather than crash.
        log(f"  WARNING: no parsed output (stop_reason={response.stop_reason}); "
            f"falling back to raw text")
        raw = "".join(b.text for b in response.content if b.type == "text")
        if not raw.strip():
            raise RuntimeError(
                f"model returned nothing usable (stop_reason={response.stop_reason})"
            )
        result = _parse(raw)
        result["action_items"] = normalize_actions(
            result["action_items"], course, date
        )
        _save_failed_response(raw, course, date)
    log(f"  topic: {result['topic_slug']} | "
        f"{len(result['key_terms'])} terms | "
        f"{len(result['action_items'])} action items | "
        f"{response.usage.input_tokens} in / {response.usage.output_tokens} out tokens")
    return result


def render_action(item: dict | str) -> str:
    """One action item as a line of text, with its date and how we got it."""
    if isinstance(item, str):
        return item
    task = item.get("task", "").strip()
    due = item.get("due_date", "")
    if not due:
        return task
    if item.get("date_source") == "assumed":
        return f"{task} (due {due}, assumed: next class)"
    return f"{task} (due {due})"


def render_markdown(result: dict, course: str, date: str) -> str:
    """Assemble the .md document that gets uploaded to Drive."""
    lines = [
        f"# {course} — {date}",
        "",
        f"**Topic:** {result['topic_slug'].replace('-', ' ')}",
        "",
        result["summary_md"],
        "",
    ]

    if result["key_terms"]:
        lines += ["## Key terms", ""]
        for t in result["key_terms"]:
            lines.append(f"- **{t['term']}** — {t['definition']}" if t["definition"]
                         else f"- **{t['term']}**")
        lines.append("")

    lines += ["## Action items", ""]
    if result["action_items"]:
        for a in result["action_items"]:
            lines.append(f"- [ ] {render_action(a)}")
    else:
        lines.append("_None mentioned in this lecture._")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize a lecture transcript. Markdown prints to stdout."
    )
    parser.add_argument("transcript", help="path to a transcript .txt")
    parser.add_argument("course", help="course code, e.g. ACCT-4321")
    parser.add_argument("date", help="lecture date, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true",
                        help="print the raw result dict instead of markdown")
    args = parser.parse_args()

    try:
        text = Path(args.transcript).expanduser().read_text()
        result = summarize(text, args.course, args.date)
        log(f"  filename: {build_filename(args.course, args.date, result['topic_slug'])}")
        print(json.dumps(result, indent=2) if args.json
              else render_markdown(result, args.course, args.date))
    except FileNotFoundError:
        log(f"error: no such transcript: {args.transcript}")
        return 1
    except anthropic.AuthenticationError:
        log("error: ANTHROPIC_API_KEY is invalid")
        return 1
    except anthropic.RateLimitError:
        log("error: rate limited by the Claude API; try again shortly")
        return 1
    except anthropic.APIStatusError as exc:
        log(f"error: Claude API returned {exc.status_code}: {exc.message}")
        return 1
    except Exception as exc:
        log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
