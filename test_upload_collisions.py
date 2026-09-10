"""Regression tests for upload.py's filename collision handling.

Runs against an in-memory fake Drive: no network, no OAuth, and nothing
touches the real Drive. From the project root:

    .venv/bin/python test_upload_collisions.py
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

fresh_home()  # before config is imported, so nothing touches ~/.intake

from intake import upload as drive  # noqa: E402

FOLDER_MIME = drive.FOLDER_MIME
KEY = drive.RECORDING_KEY_PROPERTY


class Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeFiles:
    def __init__(self, store):
        self.store = store

    def list(self, q="", fields="", **kw):
        name = re.search(r"name = '([^']*)'", q)
        parent = re.search(r"'([^']*)' in parents", q)
        prop = re.search(r"appProperties has \{ key='[^']*' and value='([^']*)' \}", q)
        wants_folder = FOLDER_MIME in q

        out = []
        for fid, f in self.store.items():
            if f["trashed"]:
                continue
            if name and f["name"] != name.group(1):
                continue
            if parent and parent.group(1) not in f["parents"]:
                continue
            if prop and f.get("appProperties", {}).get(KEY) != prop.group(1):
                continue
            if wants_folder and f["mimeType"] != FOLDER_MIME:
                continue
            if not wants_folder and f["mimeType"] == FOLDER_MIME:
                continue
            out.append({
                "id": fid, "name": f["name"],
                "appProperties": f.get("appProperties", {}),
            })
        return Req({"files": out})

    def create(self, body=None, media_body=None, fields="", **kw):
        fid = f"id{len(self.store) + 1}"
        self.store[fid] = {
            "name": body["name"],
            "parents": body.get("parents", []),
            "mimeType": body.get("mimeType", "text/plain"),
            "appProperties": body.get("appProperties", {}),
            "trashed": False,
            "writes": 1,
        }
        return Req({"id": fid, "webViewLink": f"https://drive/{fid}"})

    def update(self, fileId=None, body=None, media_body=None, fields="", **kw):
        f = self.store[fileId]
        if body:
            f["name"] = body.get("name", f["name"])
            if "appProperties" in body:
                f["appProperties"] = body["appProperties"]
        f["writes"] += 1
        return Req({"id": fileId, "webViewLink": f"https://drive/{fileId}"})

    def get(self, fileId=None, fields="", **kw):
        f = self.store.get(fileId)
        if not f:
            raise KeyError(fileId)
        return Req({"id": fileId, "trashed": f["trashed"]})


class FakeService:
    def __init__(self):
        self.store = {
            "root": {"name": "Lecture Notes", "parents": [], "trashed": False,
                     "mimeType": FOLDER_MIME, "appProperties": {}, "writes": 1},
        }

    def files(self):
        return FakeFiles(self.store)


def docs_in(service, course="ACCT-4321"):
    """Non-folder files sitting directly in the course folder."""
    folders = {i for i, f in service.store.items() if f["mimeType"] == FOLDER_MIME
               and f["name"] == course}
    return sorted(
        f["name"] for f in service.store.values()
        if f["mimeType"] != FOLDER_MIME and set(f["parents"]) & folders
        and not f["trashed"]
    )


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    print(f"ok    {label}")
    return True


def setup(monkey_service):
    drive.get_service = lambda interactive=True: monkey_service
    drive.ensure_root_folder = lambda service: "root"


tmp = Path(tempfile.mkdtemp())
md = tmp / "summary.md"
md.write_text("# summary\n")

results = []

# 1. A recording uploaded for the first time.
def t1():
    svc = FakeService(); setup(svc)
    r = drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-03_Job-Order-Costing",
                     as_google_doc=True, recording_key="2026-09-03T14:00",
                     time_suffix="1400")
    assert r.name == "ACCT-4321_2026-09-03_Job-Order-Costing", r.name
    assert docs_in(svc) == ["ACCT-4321_2026-09-03_Job-Order-Costing"], docs_in(svc)
results.append(run("first upload creates one file", t1))

# 2. Same recording run twice with the same slug: replace, don't duplicate.
def t2():
    svc = FakeService(); setup(svc)
    for _ in range(2):
        drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-03_Job-Order-Costing",
                     as_google_doc=True, recording_key="2026-09-03T14:00",
                     time_suffix="1400")
    assert docs_in(svc) == ["ACCT-4321_2026-09-03_Job-Order-Costing"], docs_in(svc)
results.append(run("re-run of one recording replaces, no duplicate", t2))

# 3. Same recording, slug came back different. Must rename, not duplicate.
#    This is the September 3 "Lecture-Notes" case.
def t3():
    svc = FakeService(); setup(svc)
    drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-03_Lecture-Notes",
                 as_google_doc=True, recording_key="2026-09-03T15:11",
                 time_suffix="1511")
    r = drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-03_Job-Order-Costing",
                     as_google_doc=True, recording_key="2026-09-03T15:11",
                     time_suffix="1511")
    assert r.name == "ACCT-4321_2026-09-03_Job-Order-Costing", r.name
    assert docs_in(svc) == ["ACCT-4321_2026-09-03_Job-Order-Costing"], docs_in(svc)
results.append(run("re-run with a new slug renames in place", t3))

# 4. THE BUG: two different recordings of one class on one day, same slug.
def t4():
    svc = FakeService(); setup(svc)
    a = drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-01_Job-Order-Costing",
                     as_google_doc=True, recording_key="2026-09-01T14:00",
                     time_suffix="1400")
    b = drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-01_Job-Order-Costing",
                     as_google_doc=True, recording_key="2026-09-01T14:47",
                     time_suffix="1447")
    assert a.name != b.name, "both parts got the same name"
    assert b.name == "ACCT-4321_2026-09-01_Job-Order-Costing_1447", b.name
    assert len(docs_in(svc)) == 2, docs_in(svc)
results.append(run("two recordings, same day and slug, both survive", t4))

# 5. And a re-run of the second part still maps to its suffixed file.
def t5():
    svc = FakeService(); setup(svc)
    for key, sfx in (("2026-09-01T14:00", "1400"), ("2026-09-01T14:47", "1447"),
                     ("2026-09-01T14:47", "1447"), ("2026-09-01T14:00", "1400")):
        drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-01_Job-Order-Costing",
                     as_google_doc=True, recording_key=key, time_suffix=sfx)
    assert len(docs_in(svc)) == 2, docs_in(svc)
results.append(run("re-running either part stays at two files", t5))

# 6. Backward compatibility: a file uploaded before keys existed.
def t6():
    svc = FakeService(); setup(svc)
    course = drive.ensure_folder(svc, "ACCT-4321", "root")
    svc.store["legacy"] = {
        "name": "ACCT-4321_2026-09-02_Theories-Of-Leadership", "parents": [course],
        "mimeType": "application/vnd.google-apps.document", "appProperties": {},
        "trashed": False, "writes": 1,
    }
    r = drive.upload(md, "ACCT-4321", name="ACCT-4321_2026-09-02_Theories-Of-Leadership",
                     as_google_doc=True, recording_key="2026-09-02T10:00",
                     time_suffix="1000")
    assert r.name == "ACCT-4321_2026-09-02_Theories-Of-Leadership", r.name
    assert len(docs_in(svc)) == 1, docs_in(svc)
    assert svc.store["legacy"]["appProperties"][KEY] == "2026-09-02T10:00"
results.append(run("pre-existing unkeyed file is adopted, then keyed", t6))

# 7. Plain CLI upload with no key keeps the old replace-by-name behavior.
def t7():
    svc = FakeService(); setup(svc)
    drive.upload(md, "ACCT-4321")
    drive.upload(md, "ACCT-4321")
    assert docs_in(svc) == ["summary.md"], docs_in(svc)
results.append(run("keyless CLI upload still replaces by name", t7))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
