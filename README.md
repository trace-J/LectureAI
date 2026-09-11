# LectureAI

Record a lecture on your Mac, or drop one in the inbox from your phone. Get a
study summary as a Google Doc in the right course folder in Drive, with the raw
transcript filed one level down.

Everything in between is automatic: the course is inferred from your class
schedule, the audio is compressed and split to clear the transcription API's
size limit, Claude writes the summary, and the original recording is deleted
once both uploads land.

## How a recording flows through

```
intake panel        a local control panel over everything below
   |
   v
intake record       records from this Mac's mic into .work/, then moves the
   |                finished file into inbox/ (or sync one from your phone)
   v
inbox/lecture.m4a
   |
   |  intake watch      waits for the file to stop growing, then infers
   |                    the course and date from the recording's START time
   |                    (mtime minus duration), falling back to the filename
   v
   |  transcribe.py     compresses over ~24MB, splits over 8 minutes,
   |                    transcribes each chunk, stitches them in order
   v
   |  summarize.py      one Claude call, response constrained to a schema:
   |                    summary, key terms, action items, topic slug
   v
   |  upload.py         ACCT-4321/ACCT-4321_2026-09-03_Job-Order-Costing  (Doc)
   |                    ACCT-4321/Transcripts/..._Job-Order-Costing.txt
   v
   |  notion_tasks      action items become dated tasks in your Notion to-do
   |                    list (skipped entirely unless Notion is set up)
   v
pipeline.log        one line per lecture: timestamp, course, source file, URL
```

Everything the pipeline reads or writes lives in one place, `~/.intake`:
your keys in `.env`, your `schedule.toml`, the `inbox/` and `processed/`
folders, the Drive token, and `pipeline.log`. Set `INTAKE_HOME` to put it
somewhere else (`LECTUREAI_HOME`, the variable's old name, still works when the
new one is unset). The code never keeps anything next to itself.

Local copies are staged in `processed/` before upload and removed after, so a
failed upload never costs you the transcription you already paid for.

## Setup

You need a Mac, Homebrew, an OpenAI API key, and an Anthropic API key. Three
commands in Terminal, then the rest happens in your browser:

```bash
brew install ffmpeg pipx
```

```bash
pipx install git+https://github.com/trace-J/LectureAI
```

```bash
intake panel
```

That opens the control panel in your browser. The first time, with nothing
configured, it lands on the **Setup** page: paste your two keys, pick a
microphone from the list, add a row for every time a class meets, tick Notion
if you want it, and click **Connect Google Drive**, which opens a Google
sign-in tab. A checkup at the bottom of the page shows what still needs doing
and turns green as you go. Everything is saved on your Mac in `~/.intake`;
nothing is sent anywhere but to the services you gave keys for.

Come back to the Setup page any time from the gear in the panel's header.
Saved keys are shown masked and a blank field keeps what is there, so
changing one thing never means retyping the rest.

The same setup works from Terminal if you prefer:

```bash
intake setup
```

It asks for the two keys, shows the microphones ffmpeg can see, takes your
class schedule one line at a time (`Tue 14 ACCT-4321`, blank line when done),
asks once about Notion, and offers to authorize Google Drive at the end.

Either way, `intake doctor` is the check to run when something misbehaves.
It prints one line per thing that has to be right, with the exact fix next to
anything that is not, and it never touches the network. The Setup page's
checkup is the same list.

```
ok    Python               3.12.14
ok    ffmpeg               /opt/homebrew/bin/ffmpeg
ok    home directory       /Users/you/.intake (default)
ok    OpenAI key           sk-proj...Xk2A
ok    Anthropic key        sk-ant-...9fQ1
ok    class schedule       8 class meetings, ACCT-4321, ENTR-3306, ENTR-4306, RELI-3304, tolerance 45 min
ok    Drive OAuth client   bundled with the package (Cloud project friendly-bazaar-507320-b7)
FAIL  Drive authorization  no token.json yet
                           fix: intake login
ok    Notion               skipped in setup (optional)
ok    microphone           3 inputs, will record from MacBook Pro Microphone
```

For Drive, the Google OAuth client ships with the package, so there is no
Cloud project to create. If setup did not already do it:

```bash
intake login
```

That caches `token.json` in `~/.intake`. The app uses the `drive.file`
scope, which only reaches files it created, so it makes its own "Lecture
Notes" folder in My Drive rather than writing into one you made by hand. Move
that folder anywhere afterward; access follows it.

If `pipx` says the command is not on your PATH, run `pipx ensurepath` and open
a new terminal. To update later: `pipx upgrade intake`.

If you installed when the command was still called `lectureai`, pipx knows the
package by that name and `upgrade` will not carry it across the rename. Run
`pipx uninstall lectureai`, then the install line above. The old command keeps
working as a second name for `intake` for now, and your data is picked up as
described under "You installed when this was called lectureai" below.

## The control panel

```bash
intake panel
```

It opens <http://127.0.0.1:5173> in your browser (add `--no-browser` to skip
that). One page to start and stop a recording,
start and stop the watcher, see what's waiting in the inbox, and open recent
lectures in Drive. It drives the same modules the CLI does, so a recording
started here is identical to one started with `intake record`.

While a lecture is being processed the panel shows the stage it is in:
waiting for the file to finish copying, transcribing (with the part number, so
a ten-part lecture visibly advances), summarizing, uploading, adding tasks to
Notion. A 75 minute recording takes several minutes and ten API calls, and
without this it just sits in the inbox looking untouched, because
`pipeline.log` gets its line only once the whole thing is done.

The watcher writes that stage to `.work/status.json` and stamps it with its
own pid; the panel ignores a status whose pid is not the watcher currently
running, so a watcher killed mid-lecture cannot leave a stage on screen
forever.

The theme buttons in the top right switch between **system, light, and
dark**. System follows macOS; picking light or dark overrides it and is
remembered in that browser. Every color is defined once with CSS
`light-dark()`, so the two themes cannot drift apart the way a duplicated
palette does.

It binds to localhost only, and deliberately: it can start and stop processes
and read your pipeline log, none of which belongs on the network. The setup
row at the bottom reports which pieces are configured by presence alone, so no
key or token is ever sent to the browser.

Both the recorder and the watcher are started detached, on purpose, so
closing the panel abandons neither. A recording keeps going if the panel
quits or is killed mid-lecture; the next panel (or `intake record`) finds
it through `.work/recording.json`, shows it as live with a note that it was
picked up, and the stop button files it as usual. If nobody ever comes back,
ffmpeg stops itself at `RECORD_MAX_MINUTES` (240) and writes a valid file,
which the next panel files into the inbox on its first status poll. The
watcher likewise finishes a lecture midway through transcription on its own.

Whether the watcher is running is read from its lock file, which it holds
for exactly as long as it lives. The pid inside is only a label: a watcher
the panel started and that was later killed stays a zombie that still
answers a liveness check, and the panel once reported one as running for an
hour while nothing was processing. The lock cannot do that, and the panel
also reaps the watchers it started.

A recording that captures nothing is called out in two places. While it is
running, a file that still holds no audio a minute in turns the timer red
with a note under it, because a microphone that is open but blocked (usually
a permission never granted to whatever launched the panel) looks exactly
like a healthy one otherwise: the timer ticks either way. And when it ends,
the failure is written to `pipeline.log` and shows in the recent list as
failed, with ffmpeg's own log kept in `.work/` if it said anything. A flash
on the page and a line in the terminal are gone in seconds, and a recording
found dead during a dark wake shows nobody anything.

## Recording on this Mac

```bash
intake record
```

Records until Ctrl-C, names the file from your schedule, and drops it in
`~/.intake/inbox/`. The first run asks for microphone permission; if it
was denied, grant it under System Settings > Privacy & Security > Microphone
and try again.

```bash
intake record --minutes 80          # stop on its own
intake record --course RELI-3304    # override the schedule
intake record --device "MacBook"    # pick a different mic
intake record --list-devices
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
  in connection order, so index 0 is often a nearby iPhone rather than the
  built-in mic. `intake setup` picks by name; so does `RECORD_DEVICE` in
  `~/.intake/.env`.
- **Press Stop before closing the lid.** Sleep ends the capture. The Mac is
  held awake for as long as ffmpeg runs (`caffeinate -w`, started alongside
  it), so a dimmed display or an idle timer cannot end a lecture early, but
  nothing can hold a closed lid open. A recording cut off by sleep is still
  filed with whatever it captured up to that point.

Stopping with Ctrl-C is the supported way to end a recording: it lets ffmpeg
write the file's trailer. Killing the process instead leaves an m4a nothing can
open.

## Running it

```bash
intake watch
```

Watches the inbox until Ctrl-C. It never dies on a bad file: the error goes to
`pipeline.log`, the recording stays in `inbox/` for a retry, and the next one
still gets processed. A lock file stops two watchers from racing on the same
inbox. To stop a stray one:

```bash
pkill -f 'intake.*watch'
```

One file at a time, without the watcher:

```bash
intake watch --once ~/.intake/inbox/lecture.m4a
```

## The class schedule

`~/.intake/schedule.toml` maps each class meeting to a course code. One
row per meeting: the day, the hour it starts on a 24-hour clock, and the code.
`intake setup` writes it; editing it by hand is just as good, and is the
only thing you need to touch each semester.

```toml
classes = [
  { day = "Mon", start =  9, course = "ENTR-4306" },
  { day = "Tue", start = 12, course = "ENTR-3306" },
  { day = "Tue", start = 14, course = "ACCT-4321" },
]

tolerance_minutes = 45
```

Two details that matter:

- The match runs against when the recording **started**, not the file's mtime.
  An 80 minute class ends closer to the next class on the calendar than to its
  own, so backing out the duration is what keeps back-to-back classes apart.
- `tolerance_minutes` must stay well under the gap between consecutive
  classes. With a 12:00 and a 14:00 on the same day, anything near 60 makes
  the two windows touch and the wrong course wins. The file carries this
  warning next to the value.

If the timestamp matches nothing, the filename is tried next: a recording named
`9-1-26-acct-4321-pt2.m4a` still files correctly. Only codes already in the
schedule are accepted, so a stray number in a filename cannot invent a course
folder. Failing both, it goes to `UNKNOWN/`.

A missing or broken schedule stops `record`, `watch`, and `panel` with one
line saying which row is wrong and pointing at `intake setup`.

## Things worth knowing when it misbehaves

**Start with `intake doctor`.** It checks every piece the pipeline depends
on and prints the fix for the ones that are missing.

**A summary comes back thin or oddly formatted.** The transcript is probably
truncated. The `gpt-4o-mini-transcribe` model caps output near 2000 tokens and
truncates silently rather than erroring, which is why audio is split on
duration as well as size. Lower `CHUNK_SECONDS` in `config.py` if you see
truncation warnings.

**Uploads suddenly fail with an auth error.** The Google Cloud project behind
the bundled client is still in External + Testing, so refresh tokens expire
every 7 days and only accounts added as test users can log in at all.
`intake login` gets you going again; publishing the project ends it
permanently.

**A recording was filed under the wrong course.** Rename it with the course
code in the filename and drop it back in `inbox/`. The filename fallback will
catch it.

**The panel opens on the Setup page instead of the controls.** Both keys and
at least one class are needed before anything can be recorded and filed. Fill
them in and the panel is one click away in the header.

**The timer ran for the whole lecture and nothing reached the inbox.** The
microphone was open but delivering nothing, and the recent list shows the
lecture as failed with the reason. On macOS that is almost always the
microphone permission for whatever launched the panel or `intake record`:
the terminal app, or the app whose terminal it was. Grant it under System
Settings > Privacy & Security > Microphone. The panel now turns the timer red
about a minute in when this is happening, so check the page after you press
Record. ffmpeg's own log, if it said anything, is in `~/.intake/.work/`.

**The panel says the watcher is running but nothing gets processed.** Restart
the panel; older versions judged the watcher by its pid, which a watcher that
had been killed kept answering until the panel that started it quit.

**You installed before the home directory existed.** Your `.env`, token, and
log are still next to the code. The first `intake` command you run offers
to move them into `~/.intake`; say yes and restart any watcher or panel that
was already running.

**You installed when this was called lectureai.** Your data is in
`~/.lectureai`. The first `intake` command you run at a terminal offers to
move your `.env`, `schedule.toml`, Drive token, log, and any recordings in
`inbox/` and `processed/` into `~/.intake`; the panel does not ask, so use
`intake setup` or `intake doctor` for that first run. Until you say yes,
`intake doctor` lists what is waiting under "older install"; afterward it
reports the old folder under "older home", and you can delete it once
nothing is recording into its `.work/`. Scratch files in `.work/` are not
moved. If you had `LECTUREAI_HOME` set, nothing moves: the variable is still
read, and the doctor line for the home directory suggests renaming it.

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
database, dated. This is optional: skip it in `intake setup` and the
pipeline behaves exactly as it did before.

**Setup**, which is mostly in the browser:

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy
   its secret.
2. Open your to-do database in Notion, and under the `...` menu choose
   **Connections** and add that integration. Without this step every request
   comes back 404, because the integration cannot see a database nobody shared
   with it.
3. Give both values to `intake setup` when it asks, or put them in
   `~/.intake/.env` yourself:

```
NOTION_TOKEN=ntn_...
NOTION_DATABASE=https://www.notion.so/...   # just paste the database URL
```

Then confirm it can see your database and worked out the right properties:

```bash
intake notion --check
```

That prints every property in your database and which role it was matched to.

`--setup` below only applies to `NOTION_TARGET=database`; it refuses while
the target is `weekly`, since checkboxes never read those properties and
adding them would only leave clutter in your database.

If it reports that no date property matched, your database has nowhere to put
a deadline. Add what's needed:

```bash
intake notion --setup --dry-run   # see what it would add
intake notion --setup
```

That adds `Due` (date), `Course` (select, pre-filled with the codes in your
schedule), and `Source` (url) **only where the role isn't already covered**.
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
`~/.intake/.env`:

```
NOTION_PROP_DUE=Deadline
NOTION_PROP_COURSE=Class
```

**Where tasks land.** A Notion database has two entirely separate surfaces,
and this matters more than it sounds. Database **rows** have real Due and
Course fields. Page **blocks** are where a weekly checklist actually lives: a
page per week, a column per day, checkboxes inside. A row is invisible from
the weekly page and vice versa, so a task can be filed perfectly and still be
nowhere you would ever see it. That happened.

By default (`NOTION_TARGET=weekly`) each action item becomes a **checkbox in
the day column matching its due date**, prefixed with the course and carrying
a link back to the Drive summary. Set `NOTION_TARGET=database` to create rows
with Due/Course/Source fields instead.

The weekly page is found by its date heading, e.g. `Sep 9 - Sep 13`, matched
against the task's due date. Headings carry no year, so the year is inferred
from the date being filed and checked against its neighbours, which is what
keeps the last week of December working. **If no page covers that date, the
task is skipped and reported** rather than filed into whatever page happens to
exist: a task hidden in a week you already finished is worse than one that
never arrived. Duplicate the weekly page and set its heading, and the next run
picks it up.

Within a day column, a blank checkbox from the template is filled before any
new one is appended, so the column keeps the shape you set up.

Run `intake notion --check` to see every page, the date range read from
each heading, and where the next seven days would land.

**Dates.** A deadline the instructor actually stated is used as-is, with
anything relative ("next Thursday") resolved against the lecture date. An item
with no stated deadline is dated to the **next time that class meets**,
computed from your schedule, and labeled `assumed` in the Drive summary so you
can tell the two apart. Nothing lands undated, because an undated task has no
day column to go in and gets skipped.

**Re-runs don't duplicate.** Before adding anything, the database is checked
for a task with the same title and due date. Re-processing a lecture adds
nothing the second time.

**Failures are contained.** Notion runs last, after Drive, and never raises. A
bad token or an outage costs you the Notion tasks for that lecture and nothing
else; the action items are still in the summary Doc.

## Development

The code is a plain Python package in `intake/`, and the `intake`
command is one console script over it (`lectureai` is a second name for the
same entry point, kept for now). To work on it:

```bash
git clone https://github.com/trace-J/LectureAI && cd LectureAI
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # installs the package editable, with the intake command
```

Dependencies are declared once, in `pyproject.toml`; `requirements.txt` just
points at it. Python 3.11 or newer, for `tomllib`.

Every module still runs on its own from the checkout, which is how to debug a
single stage. The root-level `record.py`, `watch.py`, `gui.py`, `upload.py`,
`transcribe.py`, `summarize.py`, and `notion_tasks.py` are one-line shims onto
the package, so these are the same code the CLI runs:

```bash
.venv/bin/python record.py --list-devices
.venv/bin/python watch.py --once ~/.intake/inbox/lecture.m4a
.venv/bin/python transcribe.py lecture.m4a > transcript.txt
.venv/bin/python summarize.py transcript.txt ACCT-4321 2026-09-03
.venv/bin/python upload.py summary.md ACCT-4321
.venv/bin/python gui.py
```

A dev checkout and a pipx install share `~/.intake` by default. To keep a
scratch install from touching your real recordings and keys:

```bash
INTAKE_HOME=/tmp/intake-scratch intake setup
```

The Google OAuth client the app identifies itself with is `credentials.json`
inside the package, read by `google_client.py`. A `credentials.json` in the
home directory overrides it. The one at the repo root, if you have one from
before, stays gitignored.

### Tests

Every suite is self-contained: the upload tests run against an in-memory fake
Drive, the recording tests open no microphone, the setup tests drive the
wizard with scripted answers into a temp directory. None needs network,
credentials, or an API key, none costs anything to run, and none can touch
`~/.intake`.

```bash
.venv/bin/python test_upload_collisions.py   # collisions, re-runs, two-part days
.venv/bin/python test_record.py              # device selection and naming
.venv/bin/python test_notion_tasks.py        # property mapping and de-duplication
.venv/bin/python test_gui.py                 # log parsing and API guard rails
.venv/bin/python test_setup.py               # home directory, schedule file, setup wizard, doctor
```

## V2 roadmap

Everything described above is built and in daily use. These are what's left,
in rough order of how soon each one bites.

### New capability

- **Speaker diarization.** Separate the instructor from student questions.
- **Cross-lecture study guides.** Synthesize a whole unit rather than one
  lecture. The highest-value item for actually studying, and the one that most
  wants a database underneath it.
- **A database.** State currently lives in `pipeline.log` and the filesystem.

Dropped: **slide OCR**, decided against on 2026-09-08 as not worth the
complexity.

### Loose ends

- **Publish the Google Cloud project.** The bundled OAuth client's project is
  still in Testing, so `intake login` only works for accounts listed as
  test users and their tokens expire weekly. Switching it to Production is
  what makes the friend path above work for anyone.
- **Recover the September 3, 13:56 ACCT lecture.** Audio and transcript are
  gone, but the summary text survives in the raw JSON of the trashed Doc.
