"""Tests for notion_tasks.py's id parsing, property mapping, and push logic.

Every Notion call is stubbed, so this needs no token, no database, and no
network. From the project root:

    .venv/bin/python test_notion_tasks.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import notion_tasks as nt  # noqa: E402

# A to-do database shaped the way a real one tends to be: a title, a couple of
# date properties, a status, and some selects.
SCHEMA = {
    "Task": {"type": "title"},
    "Due": {"type": "date"},
    "Created": {"type": "date"},
    "Status": {"type": "status"},
    "Course": {"type": "select"},
    "Type": {"type": "multi_select"},
    "Source": {"type": "url"},
    "Notes": {"type": "rich_text"},
}


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


def reset_overrides():
    # A stand-in database id so resolve_data_source gets past id parsing;
    # every request it would make is stubbed below.
    config.NOTION_DATABASE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    config.NOTION_TOKEN = "test-token"
    config.NOTION_PROP_DUE = ""
    config.NOTION_PROP_COURSE = ""
    config.NOTION_PROP_KIND = ""
    config.NOTION_PROP_SOURCE = ""


results = []

# --- id extraction --------------------------------------------------------

def t1():
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    url = f"https://www.notion.so/myspace/{bare}?v=99887766554433221100aabbccddeeff"
    assert nt.extract_id(url) == bare, nt.extract_id(url)
    assert nt.extract_id(bare) == bare
    dashed = "a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6"
    assert nt.extract_id(dashed) == bare, nt.extract_id(dashed)
results.append(run("pulls the database id out of a URL, bare id, or dashed id", t1))


def t2():
    # The view id after ?v= must not win over the database id.
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    view = "99887766554433221100aabbccddeeff"
    assert nt.extract_id(f"https://notion.so/{bare}?v={view}") == bare
results.append(run("the view id in a URL is not mistaken for the database", t2))


def t2b():
    # Notion puts the page title in the URL ahead of the id, and a title made
    # of hex-ish words ("Deface Added Beef Cafe") used to be matched as part
    # of the id, yielding a database that doesn't exist.
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    for title in ("Deface-Added-Beef-Cafe", "Faced-Badace-Decade-Beaded-Cabbage",
                  "Accede-Facade-Deface"):
        url = f"https://www.notion.so/myspace/{title}-{bare}?v=99887766554433221100aabbccddeeff"
        assert nt.extract_id(url) == bare, f"{title}: {nt.extract_id(url)}"
results.append(run("a hex-looking page title is not mistaken for the id", t2b))


def t2c():
    bare = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    dashed = "a1b2c3d4-e5f6-a7b8-c9d0-e1f2a3b4c5d6"
    view = "99887766554433221100aabbccddeeff"
    for label, url in (
        ("fragment", f"https://www.notion.so/myspace/Tasks-{bare}#block"),
        ("no scheme", f"notion.so/myspace/Tasks-{bare}?v={view}"),
        ("dashed in a URL", f"https://www.notion.so/myspace/{dashed}?v={view}"),
        ("nested path", f"https://www.notion.so/team/{view}/{bare}?v={view}"),
    ):
        assert nt.extract_id(url) == bare, f"{label}: {nt.extract_id(url)}"
results.append(run("handles fragments, nested paths, and dashed ids in URLs", t2c))


def t3():
    for bad in ("", "https://notion.so/my-page", "not an id"):
        try:
            nt.extract_id(bad)
        except nt.NotionError:
            continue
        raise AssertionError(f"{bad!r} should have raised")
results.append(run("a URL with no id raises a readable error", t3))


# --- property mapping -----------------------------------------------------

def t4():
    reset_overrides()
    m = nt.map_properties(SCHEMA)
    assert m["title"] == "Task", m
    assert m["due"] == "Due", m          # "Due" beats "Created"
    assert m["course"] == "Course", m
    assert m["kind"] == "Type", m
    assert m["source"] == "Source", m
results.append(run("maps a typical to-do database by property name", t4))


def t5():
    reset_overrides()
    config.NOTION_PROP_DUE = "Created"
    m = nt.map_properties(SCHEMA)
    assert m["due"] == "Created", m
    reset_overrides()
results.append(run("an explicit override wins over the name hints", t5))


def t6():
    reset_overrides()
    config.NOTION_PROP_DUE = "Nonexistent"
    try:
        nt.map_properties(SCHEMA)
    except nt.NotionError as exc:
        assert "not in the database" in str(exc), exc
    else:
        raise AssertionError("a bogus override should raise")
    reset_overrides()
results.append(run("an override naming a missing property raises", t6))


def t7():
    reset_overrides()
    # No name hint matches, but there is exactly one date property.
    sparse = {"Thing": {"type": "title"}, "Whenever": {"type": "date"}}
    m = nt.map_properties(sparse)
    assert m["due"] == "Whenever", m
    assert m["course"] is None and m["kind"] is None, m
results.append(run("a lone date property is used even with no matching name", t7))


def t8():
    reset_overrides()
    # Nothing but a title: everything else is skipped, not invented.
    m = nt.map_properties({"Name": {"type": "title"}})
    assert m["title"] == "Name"
    assert all(m[r] is None for r in ("due", "course", "kind", "source")), m
results.append(run("a title-only database maps cleanly with no extras", t8))


def t9():
    reset_overrides()
    try:
        nt.map_properties({"Due": {"type": "date"}})
    except nt.NotionError as exc:
        assert "title" in str(exc), exc
    else:
        raise AssertionError("a schema with no title should raise")
results.append(run("a database with no title property raises", t9))


def t10():
    assert nt._value_for("select", "ACCT-4321") == {"select": {"name": "ACCT-4321"}}
    assert nt._value_for("multi_select", "reading") == {
        "multi_select": [{"name": "reading"}]}
    assert nt._value_for("status", "Todo") == {"status": {"name": "Todo"}}
    assert nt._value_for("url", "https://x") == {"url": "https://x"}
    rich = nt._value_for("rich_text", "hello")
    assert rich["rich_text"][0]["text"]["content"] == "hello", rich
results.append(run("each property type gets the right value shape", t10))


# --- push -----------------------------------------------------------------

class FakeNotion:
    """Stands in for _request, recording what would have been sent."""

    def __init__(self, existing_titles=()):
        self.existing = set(existing_titles)
        self.created = []

    def __call__(self, method, path, payload=None):
        if method == "GET" and path.startswith("/databases/"):
            return {"title": [{"plain_text": "My Tasks"}],
                    "data_sources": [{"id": "ds-1", "name": "My Tasks"}]}
        if method == "GET" and path.startswith("/data_sources/"):
            return {"properties": SCHEMA}
        if method == "POST" and path.endswith("/query"):
            wanted = self._title_from_filter(payload.get("filter", {}))
            return {"results": [{"id": "existing"}] if wanted in self.existing else []}
        if method == "POST" and path == "/pages":
            self.created.append(payload)
            return {"url": f"https://notion.so/page-{len(self.created)}"}
        raise AssertionError(f"unexpected call: {method} {path}")

    @staticmethod
    def _title_from_filter(f):
        for clause in f.get("and", [f]):
            if "title" in clause:
                return clause["title"]["equals"]
        return None


ITEMS = [
    {"task": "Read chapter 7", "due_date": "2026-09-15",
     "kind": "reading", "date_source": "stated"},
    {"task": "Problem set 3", "due_date": "2026-09-10",
     "kind": "assignment", "date_source": "assumed"},
]


def t11():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321", source_url="https://drive/doc")
    assert out["added"] == 2 and out["skipped"] == 0, out
    assert len(fake.created) == 2, fake.created

    first = fake.created[0]
    assert first["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}, first
    props = first["properties"]
    assert props["Task"]["title"][0]["text"]["content"] == "Read chapter 7"
    assert props["Due"]["date"]["start"] == "2026-09-15", props
    assert props["Course"]["select"]["name"] == "ACCT-4321", props
    assert props["Type"]["multi_select"][0]["name"] == "reading", props
    assert props["Source"]["url"] == "https://drive/doc", props
    # Status is left alone so the database's own default applies.
    assert "Status" not in props, props
results.append(run("push creates one page per item with mapped properties", t11))


def t12():
    reset_overrides()
    fake = FakeNotion(existing_titles={"Read chapter 7"})
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321")
    assert out["added"] == 1 and out["skipped"] == 1, out
    assert len(fake.created) == 1, fake.created
    assert fake.created[0]["properties"]["Task"]["title"][0]["text"]["content"] \
        == "Problem set 3"
results.append(run("an item already in the database is skipped, not duplicated", t12))


def t13():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    out = nt.push([], "ACCT-4321")
    assert out == {"added": 0, "skipped": 0, "failed": 0, "urls": []}, out
    assert fake.created == [], "a lecture with no action items must not call Notion"
results.append(run("no action items means no Notion calls at all", t13))


def t14():
    reset_overrides()
    fake = FakeNotion()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321", dry_run=True)
    assert out["added"] == 2, out
    assert fake.created == [], "dry run must not create anything"
results.append(run("dry run reports without writing", t14))


def t15():
    reset_overrides()
    # An item that fails must not stop the ones after it.
    class Flaky(FakeNotion):
        def __call__(self, method, path, payload=None):
            if method == "POST" and path == "/pages" and not self.created:
                self.created.append(None)
                raise nt.NotionError("simulated failure")
            return super().__call__(method, path, payload)

    fake = Flaky()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321")
    assert out["failed"] == 1 and out["added"] == 1, out
results.append(run("one failing item does not lose the rest", t15))


def t16():
    reset_overrides()
    # A database with no date property still takes the tasks, just undated.
    sparse = {"Name": {"type": "title"}}

    class NoDates(FakeNotion):
        def __call__(self, method, path, payload=None):
            if method == "GET" and path.startswith("/data_sources/"):
                return {"properties": sparse}
            return super().__call__(method, path, payload)

    fake = NoDates()
    nt._request = fake
    out = nt.push(ITEMS, "ACCT-4321")
    assert out["added"] == 2, out
    assert list(fake.created[0]["properties"]) == ["Name"], fake.created[0]
results.append(run("a database without a date property still gets the tasks", t16))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
