# LectureAI

Record a lecture on this Mac, or drop one in `inbox/` from your phone. Get a
study summary as a Google Doc in the right course folder in Drive, with the raw
transcript filed one level down.

Everything in between is automatic: the course is inferred from your class
schedule, the audio is compressed and split to clear the transcription API's
size limit, Claude writes the summary, and the original recording is deleted
once both uploads land.

## How a recording flows through

```
record.py           records from this Mac's mic into .work/, then moves the
   |                finished file into inbox/ (or sync one from your phone)
   v
inbox/lecture.m4a
   |
   |  watch.py      waits for the file to stop growing, then infers
   |                the course and date from the recording's START time
   |                (mtime minus duration), falling back to the filename
   v
   |  transcribe.py compresses over ~24MB, splits over 8 minutes,
   |                transcribes each chunk, stitches them in order
   v
   |  summarize.py  one Claude call, response constrained to a schema:
   |                summary, key terms, action items, topic slug
   v
   |  upload.py     ACCT-4321/ACCT-4321_2026-09-03_Job-Order-Costing  (Doc)
   |                ACCT-4321/Transcripts/..._Job-Order-Costing.txt
   v
   |  notion_tasks  action items become dated tasks in your Notion to-do
   |                list (skipped entirely unless NOTION_TOKEN is set)
   v
pipeline.log        one line per lecture: timestamp, course, source file, URL
```

Local copies are staged in `processed/` before upload and removed after, so a
failed upload never costs you the transcription you already paid for.

## Setup

Python 3.11+ and `ffmpeg` on your PATH.

```bash
brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in OPENAI_API_KEY and ANTHROPIC_API_KEY
```

For Drive, put your Google OAuth client at `credentials.json`, then authorize
once:

```bash
.venv/bin/python upload.py --login
```

That caches `token.json`. The app uses the `drive.file` scope, which only
reaches files it created, so it makes its own "Lecture Notes" folder in My Drive
rather than writing into one you made by hand. Move that folder anywhere
afterward; access follows it. Its id is cached in `.drive_root`.

## Recording on this Mac

```bash
.venv/bin/python record.py
```

Records until Ctrl-C, names the file from your schedule, and drops it in
`inbox/`. The first run asks for microphone permission; if it was denied,
grant it under System Settings > Privacy & Security > Microphone and try
again.

```bash
.venv/bin/python record.py --minutes 80          # stop on its own
.venv/bin/python record.py --course RELI-3304    # override the schedule
.venv/bin/python record.py --device "MacBook"    # pick a different mic
.venv/bin/python record.py --list-devices
```

Three things worth knowing:

- **The mic takes a second or two to open**, so the first moments of a lecture
  are lost. Start recording before the professor does. The finished file
  reports how much audio it actually holds, not how long you sat there.
- **Recording goes to `.work/` and only moves into `inbox/` once finalized.**
  An in-progress recording is not a valid m4a, and an 80 minute lecture written
  directly into `inbox/` would also outlast the watcher's one hour patience for
  a file that is still growing.
- **The microphone is chosen by name, not index.** avfoundation numbers devices
  in connection order, so on this Mac index 0 is often a nearby iPhone rather
  than the built-in mic. Change the default with `RECORD_DEVICE` in `.env`.

Stopping with Ctrl-C is the supported way to end a recording: it lets ffmpeg
write the file's trailer. Killing the process instead leaves an m4a nothing can
open.

## Running it

```bash
.venv/bin/python watch.py
```

Watches `inbox/` until Ctrl-C. It never dies on a bad file: the error goes to
`pipeline.log`, the recording stays in `inbox/` for a retry, and the next one
still gets processed. A lock file stops two watchers from racing on the same
inbox. To stop a stray one:

```bash
pkill -f 'watch\.py'
```

One file at a time, without the watcher:

```bash
.venv/bin/python watch.py --once inbox/lecture.m4a
```

Each module also runs standalone, which is how to debug a single stage:

```bash
.venv/bin/python transcribe.py lecture.m4a > transcript.txt
.venv/bin/python summarize.py transcript.txt ACCT-4321 2026-09-03
.venv/bin/python upload.py summary.md ACCT-4321
```

## The class schedule

`SCHEDULE` in [config.py](config.py) maps `(day, start hour)` to a course code.
Editing it is the only thing you need to touch each semester.

Two details that matter:

- The match runs against when the recording **started**, not the file's mtime.
  An 80 minute class ends closer to the next class on the calendar than to its
  own, so backing out the duration is what keeps back-to-back classes apart.
- `SCHEDULE_TOLERANCE_MINUTES` must stay well under the gap between
  consecutive classes. With a 12:00 and a 14:00 on the same day, anything near
  60 makes the two windows touch and the wrong course wins.

If the timestamp matches nothing, the filename is tried next: a recording named
`9-1-26-acct-4321-pt2.m4a` still files correctly. Only codes already in
`SCHEDULE` are accepted, so a stray number in a filename cannot invent a course
folder. Failing both, it goes to `UNKNOWN/`.

## Things worth knowing when it misbehaves

**A summary comes back thin or oddly formatted.** The transcript is probably
truncated. The `gpt-4o-mini-transcribe` model caps output near 2000 tokens and
truncates silently rather than erroring, which is why audio is split on
duration as well as size. Lower `CHUNK_SECONDS` if you see truncation warnings.

**Uploads suddenly fail with an auth error.** If the Google Cloud project is
still in External + Testing, refresh tokens expire every 7 days.
`.venv/bin/python upload.py --login` gets you going again; switching the
project to an Internal audience ends it permanently.

**A recording was filed under the wrong course.** Rename it with the course
code in the filename and drop it back in `inbox/`. The filename fallback will
catch it.

## Re-runs, duplicates, and two-part lectures

Every uploaded file is stamped with the minute its recording started, in a
private Drive property. That stamp, not the filename, is what identifies a
lecture, which gives three useful behaviors:

- **Re-running a recording replaces its own files** rather than piling up
  copies, even if the topic slug came back different the second time and the
  filename changed with it.
- **Two recordings of one class on one day both survive.** They produce the
  same `{COURSE}_{DATE}_{Topic-Slug}`, so the second one is filed with its
  start time appended: `ACCT-4321_2026-09-01_Job-Order-Costing_1447`.
- **Nothing is overwritten unless it can be proven to be the same recording.**
  A duplicate in Drive is cheap; a lost lecture is not.

Files uploaded before this existed carry no stamp. The first time you re-run
one of those lectures, the file is claimed by name and stamped from then on.

## Action items in Notion

Every deadline a lecture mentions becomes a task in your Notion to-do
database, dated. This is optional: with `NOTION_TOKEN` unset the pipeline
skips Notion and behaves exactly as it did before.

**Setup**, which is mostly in the browser:

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy
   its secret.
2. Open your to-do database in Notion, and under the `...` menu choose
   **Connections** and add that integration. Without this step every request
   comes back 404, because the integration cannot see a database nobody shared
   with it.
3. Put both values in `.env`:

```
NOTION_TOKEN=ntn_...
NOTION_DATABASE=https://www.notion.so/...   # just paste the database URL
```

Then confirm it can see your database and worked out the right properties:

```bash
.venv/bin/python notion_tasks.py --check
```

That prints every property in your database and which role it was matched to.

If it reports that no date property matched, your database has nowhere to put
a deadline. Add what's needed:

```bash
.venv/bin/python notion_tasks.py --setup --dry-run   # see what it would add
.venv/bin/python notion_tasks.py --setup
```

That adds `Due` (date), `Course` (select, pre-filled with the codes in
`SCHEDULE`), and `Source` (url) **only where the role isn't already covered**.
It never touches a property you already have: the request is built from
scratch and mentions only new names, because Notion deletes a property that is
sent as null. A database that already has a `Deadline` field keeps it and gets
no `Due`.

Apart from `--setup`, nothing about your database's shape is assumed. The
title is the only property Notion guarantees, and a due date, course, type,
and source link are used **only if** something suitable exists. Your `Status`
property is left alone so new tasks get whatever default your workflow already
uses. Each property serves one role only: if a single select has to cover both
course and type, course wins, since knowing the class matters more than
knowing it was a reading. If a guess is wrong, override it by exact name in
`.env`:

```
NOTION_PROP_DUE=Deadline
NOTION_PROP_COURSE=Class
```

**Dates.** A deadline the instructor actually stated is used as-is, with
anything relative ("next Thursday") resolved against the lecture date. An item
with no stated deadline is dated to the **next time that class meets**,
computed from `SCHEDULE`, and labeled `assumed` in the Drive summary so you can
tell the two apart. Nothing lands undated, because an undated task sinks in a
list sorted by date.

**Re-runs don't duplicate.** Before adding anything, the database is checked
for a task with the same title and due date. Re-processing a lecture adds
nothing the second time.

**Failures are contained.** Notion runs last, after Drive, and never raises. A
bad token or an outage costs you the Notion tasks for that lecture and nothing
else; the action items are still in the summary Doc.

## Tests

Both suites are self-contained. The upload tests run against an in-memory fake
Drive and the recording tests open no microphone, so neither needs network,
credentials, or an API key, and neither costs anything to run.

```bash
.venv/bin/python test_upload_collisions.py   # collisions, re-runs, two-part days
.venv/bin/python test_record.py              # device selection and naming
.venv/bin/python test_notion_tasks.py        # property mapping and de-duplication
```

## V2 roadmap

Everything described above is built and in daily use. These are what's left,
in rough order of how soon each one bites.

### New capability

- **Speaker diarization.** Separate the instructor from student questions.
- **Cross-lecture study guides.** Synthesize a whole unit rather than one
  lecture. The highest-value item for actually studying, and the one that most
  wants a database underneath it.
- **A GUI.** CLI-only today.
- **A database.** State currently lives in `pipeline.log` and the filesystem.

Dropped: **slide OCR**, decided against on 2026-09-08 as not worth the
complexity.

### Loose ends

- **Recover the September 3, 13:56 ACCT lecture.** Audio and transcript are
  gone, but the summary text survives in the raw JSON of the trashed Doc.
- **`pydantic` is imported by [summarize.py](summarize.py) but missing from
  `requirements.txt`.** It installs today only because `anthropic` pulls it in.
