# LectureAI

Drop a lecture recording in `inbox/`. Get a study summary as a Google Doc in the
right course folder in Drive, with the raw transcript filed one level down.

Everything in between is automatic: the course is inferred from your class
schedule, the audio is compressed and split to clear the transcription API's
size limit, Claude writes the summary, and the original recording is deleted
once both uploads land.

## How a recording flows through

```
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

## V2 roadmap

V1 is what's described above and it runs daily. These are the deferrals, in
rough order of how soon each one bites.

### Fix before recording another two-part lecture

- **Filename collisions.** The upload name is
  `{COURSE}_{DATE}_{Topic-Slug}`, and [upload.py](upload.py) replaces a file of
  the same name rather than creating a second one. Two recordings of the same
  class on the same day overwrite each other. The September 1 two-part lecture
  only survived because the halves happened to produce different topic slugs.

### New capability

- **Record on this computer.** Today a recording has to be made on the phone
  and synced into `inbox/`. Record directly from the Mac's microphone instead:
  start and stop a lecture from the CLI, write straight into `inbox/`, and let
  the existing watcher take it from there. Removes the phone, the sync wait,
  and the file-stability delay from the loop.
- **Speaker diarization.** Separate the instructor from student questions.
- **Slide OCR.** Pull text off the slides and fold it into the summary.
- **Cross-lecture study guides.** Synthesize a whole unit rather than one
  lecture. The highest-value item for actually studying, and the one that most
  wants a database underneath it.
- **Notion integration.** A destination besides Drive.
- **A GUI.** CLI-only today.
- **A database.** State currently lives in `pipeline.log` and the filesystem.

### Loose ends

- **Recover the September 3, 13:56 ACCT lecture.** Audio and transcript are
  gone, but the summary text survives in the raw JSON of the trashed Doc.
- **`pydantic` is imported by [summarize.py](summarize.py) but missing from
  `requirements.txt`.** It installs today only because `anthropic` pulls it in.
