"""Transcript -> summary, key terms, and action items, via Claude.

One API call per recording. The schema the model is held to and the prompt it
is given are the active profile's (schemas.py): a study summary for Syllabus,
call notes for Sous. Everything downstream (filenames, the uploaded .md) is
built from the result plus the schedule lookup.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import anthropic

from intake import config, schemas

MAX_SLUG_WORDS = 4


def fallback_slug() -> str:
    """Topic slug when the model gave none: Lecture-Notes, or the profile's."""
    return config.PROFILE.fallback_slug


# Kept for anything that still reads the constant; the pipeline asks the profile.
FALLBACK_SLUG = fallback_slug()


# The lecture schema and prompt, importable from here as they always were.
# summarize() itself asks the active profile, which is how Sous gets its own.
KeyTerm = schemas.KeyTerm
ActionItem = schemas.ActionItem
LectureSummary = schemas.LectureSummary
SYSTEM_PROMPT = schemas.LECTURE_SYSTEM_PROMPT

USER_TEMPLATE = """{label}: {course}
Date: {date}

{kind} transcript:

{transcript}"""


def user_message(transcript: str, course: str, date: str) -> str:
    """The transcript framed for the model, labeled the way the profile sees it."""
    return USER_TEMPLATE.format(
        label=config.PROFILE.subject_label, course=course, date=date,
        kind=config.PROFILE.filename_prefix.capitalize(),
        transcript=transcript.strip(),
    )


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
        return fallback_slug()
    return "-".join(w.capitalize() if not w.isupper() else w
                    for w in words[:MAX_SLUG_WORDS])


def build_filename(course: str, date: str, topic_slug: str) -> str:
    """{COURSE}_{YYYY-MM-DD}_{Topic-Slug} — course comes from the schedule."""
    return f"{course}_{date}_{slugify_topic(topic_slug)}"


# The lecture kinds; the pipeline asks the profile's schema for the live set.
ACTION_KINDS = set(schemas.LectureSummary.ACTION_KINDS)


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
        if kind not in config.PROFILE.summary_schema.ACTION_KINDS:
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
            "topic_slug": fallback_slug(),
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
        system=config.PROFILE.summary_prompt,
        messages=[{
            "role": "user",
            "content": user_message(transcript, course, date),
        }],
        output_format=config.PROFILE.summary_schema,
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
        lines.append(f"_None mentioned in this {config.PROFILE.filename_prefix}._")
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize a transcript. Markdown prints to stdout."
    )
    parser.add_argument("transcript", help="path to a transcript .txt")
    parser.add_argument("course", help="course code, e.g. ACCT-4321")
    parser.add_argument("date", help="lecture date, YYYY-MM-DD")
    parser.add_argument("--json", action="store_true",
                        help="print the raw result dict instead of markdown")
    args = parser.parse_args(argv)

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
