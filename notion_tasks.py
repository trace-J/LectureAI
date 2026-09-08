"""Push a lecture's action items into a Notion to-do database.

    python notion_tasks.py --check          # show what it matched in your database
    python notion_tasks.py --dry-run <transcript.txt> ACCT-4321 2026-09-08

Nothing here is required. With NOTION_TOKEN unset the pipeline skips Notion
entirely and behaves exactly as it did before.

Notion split databases from data sources in API version 2025-09-03: a database
is now a container whose child data source holds the schema and the rows. So a
database id gets resolved to a data source id first, and pages are parented to
the data source, not the database.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

import config

API = "https://api.notion.com/v1"
TIMEOUT = 30

# A Notion id is 32 hex characters, written bare or as a dashed UUID.
#
# Both patterns are anchored so they can't match part of the page title that
# Notion puts in the URL ahead of the id. A title like "Deface Added Beef
# Cafe" is hex all the way through, and a loose pattern happily matched the
# title plus the front of the real id and returned a database that doesn't
# exist.
UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?![0-9a-fA-F])"
)
HEX32_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")

# How property names are matched when the database isn't explicitly mapped.
# First hit wins, so the more specific names come first.
DUE_HINTS = ("due", "deadline", "when", "date")
COURSE_HINTS = ("course", "class", "subject")
KIND_HINTS = ("type", "kind", "category", "label")
SOURCE_HINTS = ("source", "lecture", "notes", "link", "url", "reference")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class NotionError(RuntimeError):
    """A Notion request failed in a way worth showing the user."""


def enabled() -> bool:
    """Whether Notion is configured at all."""
    return bool(config.NOTION_TOKEN and config.NOTION_DATABASE)


def extract_id(value: str) -> str:
    """Pull a Notion id out of a URL, or pass a bare id through.

    The query string goes first, since that is where the ?v= view id lives and
    it looks exactly like a database id. Of what remains, the last match wins:
    Notion puts the id at the end of the path, after the workspace and the
    page title.
    """
    path = str(value or "").split("?")[0].split("#")[0]
    found = list(UUID_RE.finditer(path)) + list(HEX32_RE.finditer(path))
    if not found:
        raise NotionError(
            f"could not find a Notion id in {value!r}. Open the database in "
            f"Notion, copy the URL from your browser, and paste the whole "
            f"thing."
        )
    return max(found, key=lambda m: m.start()).group(0).replace("-", "")


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    if not config.NOTION_TOKEN:
        raise NotionError(
            "NOTION_TOKEN is not set. Create an internal integration at "
            "notion.so/my-integrations, copy its secret into .env, and share "
            "your to-do database with it."
        )
    response = requests.request(
        method, f"{API}{path}",
        headers={
            "Authorization": f"Bearer {config.NOTION_TOKEN}",
            "Notion-Version": config.NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=payload, timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("message") or body.get("code") or ""
        except ValueError:
            detail = response.text[:300]
        if response.status_code == 404:
            detail += (
                "  (a 404 usually means the integration hasn't been given "
                "access: open the database in Notion, Connections, and add "
                "your integration)"
            )
        raise NotionError(f"Notion returned {response.status_code}: {detail}")
    return response.json()


def resolve_data_source(database: str = "") -> tuple[str, str]:
    """(data_source_id, title) for the configured database.

    Since 2025-09-03 the schema and rows live on a data source hanging off the
    database, so this hop is required before anything can be read or written.
    """
    # Check the token first: a missing one is the likelier problem, and
    # complaining about the database id sends you looking in the wrong place.
    if not config.NOTION_TOKEN:
        raise NotionError(
            "NOTION_TOKEN is not set. Create an internal integration at "
            "notion.so/my-integrations, put its secret in .env, then share "
            "your to-do database with it (database ... menu, Connections)."
        )
    if not (database or config.NOTION_DATABASE):
        raise NotionError(
            "NOTION_DATABASE is not set. Paste your to-do database's URL "
            "into .env."
        )
    database_id = extract_id(database or config.NOTION_DATABASE)
    info = _request("GET", f"/databases/{database_id}")
    sources = info.get("data_sources") or []
    if not sources:
        raise NotionError(
            f"database {database_id} reports no data sources, so there is "
            f"nowhere to add tasks."
        )
    if len(sources) > 1:
        log(f"  database has {len(sources)} data sources; using "
            f"{sources[0].get('name')!r}")
    title = "".join(t.get("plain_text", "") for t in info.get("title", []))
    return sources[0]["id"], title or "(untitled)"


def get_schema(data_source_id: str) -> dict:
    """The data source's property definitions, keyed by property name."""
    return _request("GET", f"/data_sources/{data_source_id}").get("properties", {})


def _find(properties: dict, hints: tuple[str, ...], types: tuple[str, ...],
          override: str = "", taken: set[str] | None = None) -> str | None:
    """Name of the property to use for one role, or None if there isn't one.

    `taken` holds properties already claimed by an earlier role. Without it a
    database with a single select property gave that property to both `course`
    and `kind`, and since both are written on create, the course code was
    immediately overwritten by the item type.
    """
    if override:
        if override in properties:
            return override
        raise NotionError(
            f"property {override!r} is not in the database. Available: "
            f"{', '.join(sorted(properties)) or '(none)'}"
        )
    claimed = taken or set()
    candidates = {name: spec for name, spec in properties.items()
                  if spec.get("type") in types and name not in claimed}
    for hint in hints:
        for name in candidates:
            if hint in name.lower():
                return name
    # A database with exactly one unclaimed property of the right type needs
    # no hint to be unambiguous.
    return next(iter(candidates)) if len(candidates) == 1 else None


def map_properties(properties: dict) -> dict:
    """Work out which property plays which role in this particular database.

    Databases are personal, so nothing is assumed beyond the title, which
    Notion guarantees exists exactly once. Anything unmatched is skipped rather
    than created: adding properties to someone's to-do list is not this
    script's business.
    """
    title = next((name for name, spec in properties.items()
                  if spec.get("type") == "title"), None)
    if not title:
        raise NotionError("database has no title property, which Notion requires")

    # Claimed in priority order, each role excluding what earlier ones took.
    # Course before kind: knowing which class a task belongs to matters more
    # than labelling it a reading, so it wins a single shared select.
    mapping = {"title": title}
    taken = {title}
    for role, hints, types, override in (
        ("due", DUE_HINTS, ("date",), config.NOTION_PROP_DUE),
        ("course", COURSE_HINTS, ("select", "multi_select", "status"),
         config.NOTION_PROP_COURSE),
        ("kind", KIND_HINTS, ("select", "multi_select"), config.NOTION_PROP_KIND),
        ("source", SOURCE_HINTS, ("url", "rich_text"), config.NOTION_PROP_SOURCE),
    ):
        found = _find(properties, hints, types, override, taken)
        mapping[role] = found
        if found:
            taken.add(found)
    return mapping


def _value_for(spec_type: str, value: str) -> dict:
    """Wrap a plain string in whatever shape the property type wants."""
    if spec_type == "select":
        return {"select": {"name": value}}
    if spec_type == "multi_select":
        return {"multi_select": [{"name": value}]}
    if spec_type == "status":
        return {"status": {"name": value}}
    if spec_type == "url":
        return {"url": value}
    return {"rich_text": [{"type": "text", "text": {"content": value[:2000]}}]}


def _already_there(data_source_id: str, mapping: dict, task: str,
                   due: str) -> bool:
    """Whether this task is already in the database.

    Matched on title, and on the due date too when there is a date property.
    Keeps a re-run of a lecture from posting its tasks a second time without
    writing any marker into someone else's database.
    """
    conditions: list[dict] = [{"property": mapping["title"],
                               "title": {"equals": task[:2000]}}]
    if mapping["due"] and due:
        conditions.append({"property": mapping["due"],
                           "date": {"equals": due}})

    payload = {"page_size": 1}
    payload["filter"] = (conditions[0] if len(conditions) == 1
                         else {"and": conditions})
    found = _request("POST", f"/data_sources/{data_source_id}/query", payload)
    return bool(found.get("results"))


def add_task(data_source_id: str, mapping: dict, schema: dict, item: dict,
             course: str, source_url: str = "") -> str:
    """Create one to-do page. Returns its Notion URL."""
    properties: dict = {
        mapping["title"]: {
            "title": [{"type": "text", "text": {"content": item["task"][:2000]}}]
        }
    }
    if mapping["due"] and item.get("due_date"):
        properties[mapping["due"]] = {"date": {"start": item["due_date"]}}
    if mapping["course"]:
        properties[mapping["course"]] = _value_for(
            schema[mapping["course"]]["type"], course
        )
    if mapping["kind"] and item.get("kind"):
        properties[mapping["kind"]] = _value_for(
            schema[mapping["kind"]]["type"], item["kind"]
        )
    if mapping["source"] and source_url:
        properties[mapping["source"]] = _value_for(
            schema[mapping["source"]]["type"], source_url
        )

    page = _request("POST", "/pages", {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": properties,
    })
    return page.get("url", "")


def push(items: list[dict], course: str, source_url: str = "",
         dry_run: bool = False) -> dict:
    """Add a lecture's action items to the configured Notion database.

    Returns counts of what happened. Skips items already present, so a
    re-processed lecture doesn't duplicate anything.
    """
    if not items:
        return {"added": 0, "skipped": 0, "failed": 0, "urls": []}

    data_source_id, db_title = resolve_data_source()
    schema = get_schema(data_source_id)
    mapping = map_properties(schema)

    if dry_run:
        log(f"  would add to {db_title!r}, mapped as: "
            + ", ".join(f"{role}={name!r}" for role, name in mapping.items()))

    added, skipped, failed, urls = 0, 0, 0, []
    for item in items:
        label = f"{item['task'][:60]}"
        try:
            if _already_there(data_source_id, mapping, item["task"],
                              item.get("due_date", "")):
                skipped += 1
                log(f"  already in Notion: {label}")
                continue
            if dry_run:
                added += 1
                log(f"  would add: {label} (due {item.get('due_date') or 'none'})")
                continue
            urls.append(add_task(data_source_id, mapping, schema, item,
                                 course, source_url))
            added += 1
            log(f"  added to Notion: {label}")
        except NotionError as exc:
            # One bad item must not cost the rest of the lecture's tasks.
            failed += 1
            log(f"  could not add {label}: {exc}")

    return {"added": added, "skipped": skipped, "failed": failed, "urls": urls}


def _course_options() -> list[dict]:
    """Seed the Course select with the courses already in SCHEDULE."""
    colors = ("blue", "green", "orange", "purple", "pink", "brown", "red")
    codes = sorted(set(config.SCHEDULE.values()))
    return [{"name": code, "color": colors[i % len(colors)]}
            for i, code in enumerate(codes)]


def setup_properties(dry_run: bool = False) -> dict:
    """Add the properties this script needs, for roles the database lacks.

    Only ever adds. An existing property is never included in the request, so
    nothing already in the database can be renamed, retyped, or removed by
    this (Notion deletes a property when it is sent as null, which is why the
    request is built from scratch rather than from the current schema).

    A role already covered by a differently named property is left alone: a
    database with "Deadline" does not also get a "Due".
    """
    data_source_id, title = resolve_data_source()
    schema = get_schema(data_source_id)
    mapping = map_properties(schema)

    wanted = {
        "due": ("Due", {"type": "date", "date": {}}),
        "course": ("Course", {"type": "select",
                              "select": {"options": _course_options()}}),
        "source": ("Source", {"type": "url", "url": {}}),
    }

    additions, skipped = {}, []
    for role, (name, spec) in wanted.items():
        if mapping.get(role):
            skipped.append(f"{role}: already using {mapping[role]!r}")
            continue
        if name in schema:
            skipped.append(
                f"{role}: a property called {name!r} exists but is a "
                f"{schema[name].get('type')}, so it was left alone"
            )
            continue
        additions[name] = spec

    for note in skipped:
        log(f"  skipped {note}")

    if not additions:
        log("  nothing to add; the database already has what it needs")
        return {"added": [], "database": title}

    if dry_run:
        log(f"  would add to {title!r}: {', '.join(sorted(additions))}")
        return {"added": sorted(additions), "database": title, "dry_run": True}

    _request("PATCH", f"/data_sources/{data_source_id}",
             {"properties": additions})
    log(f"  added to {title!r}: {', '.join(sorted(additions))}")
    return {"added": sorted(additions), "database": title}


def describe() -> str:
    """Human-readable report of what this script sees in your database."""
    data_source_id, db_title = resolve_data_source()
    schema = get_schema(data_source_id)
    mapping = map_properties(schema)

    lines = [f"database:    {db_title}", f"data source: {data_source_id}", "",
             "properties found:"]
    for name, spec in sorted(schema.items()):
        roles = [r for r, n in mapping.items() if n == name]
        marker = f"   <- {', '.join(roles)}" if roles else ""
        lines.append(f"  {name} ({spec.get('type')}){marker}")

    lines += ["", "mapping:"]
    for role in ("title", "due", "course", "kind", "source"):
        target = mapping.get(role)
        lines.append(f"  {role:7} {target if target else '(none: will be skipped)'}")
    if not mapping["due"]:
        lines.append("")
        lines.append("  No date property matched, so due dates cannot be set. "
                     "Set NOTION_PROP_DUE in .env to the exact name.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send lecture action items to a Notion to-do database."
    )
    parser.add_argument("--check", action="store_true",
                        help="show the database and property mapping, then exit")
    parser.add_argument("--setup", action="store_true",
                        help="add any missing Due / Course / Source properties")
    parser.add_argument("--transcript", help="a transcript to summarize and push")
    parser.add_argument("--course", help="course code, e.g. ACCT-4321")
    parser.add_argument("--date", help="lecture date, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be added without adding it")
    args = parser.parse_args()

    try:
        if args.setup:
            setup_properties(dry_run=args.dry_run)
            if not args.dry_run:
                print()
                print(describe())
            return 0

        if args.check:
            print(describe())
            return 0

        if not (args.transcript and args.course and args.date):
            parser.error("--transcript, --course and --date are required "
                         "unless --check or --setup is given")

        import summarize
        text = Path(args.transcript).expanduser().read_text()
        result = summarize.summarize(text, args.course, args.date)
        outcome = push(result["action_items"], args.course,
                       dry_run=args.dry_run)
        log(f"  {outcome['added']} added, {outcome['skipped']} already there, "
            f"{outcome['failed']} failed")
    except NotionError as exc:
        log(f"error: {exc}")
        return 1
    except requests.RequestException as exc:
        log(f"error: could not reach Notion: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
