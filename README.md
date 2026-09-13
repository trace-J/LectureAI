# LectureAI

Record a lecture on your Mac, or drop one in the inbox from your phone. Get a
study summary as a Google Doc in the right course folder in Drive, with the raw
transcript filed one level down.

The same pipeline now runs under two names. **Syllabus** is LectureAI: lectures,
a class schedule, study notes. **Sous** records client Zoom calls for the
account team: call notes instead of study notes, a "Client Calls" folder in
Drive, to-dos filed as database rows. Each is a *profile*: its own home under
`~/.intake`, its own keys, token, inbox, and log, so the two can never share a
recording or a credential. Choose one with the `syllabus` or `sous` command,
with `intake --profile sous`, or with `INTAKE_PROFILE=sous`; with nothing
said, `intake` is Syllabus. Everything below is written for Syllabus and holds
for Sous with the paths and names swapped.

Everything in between is automatic: the course is inferred from your class
schedule, the audio is compressed and split to clear the transcription API's
size limit, Claude writes the summary, and the original recording is deleted
once both uploads land.

The control panel is reachable from anywhere at
<https://maincoursemedia.com/syllabus> (it lands on
`syllabus.maincoursemedia.com`), behind a Google sign-in. The
panel still runs on the Mac that does the recording; the address is a
Cloudflare Tunnel to it, and `intake service install` is what keeps the
panel running there without a terminal. See "The panel on the web" below.
This repository is public so that the install line below can fetch from it.

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

Everything the pipeline reads or writes lives in one place, the profile's
home: `~/.intake/syllabus/` for Syllabus, `~/.intake/sous/` for Sous. Each
holds that profile's keys in `.env`, its schedule file (`schedule.toml` for
Syllabus, `calls.toml` for Sous), the `inbox/` and `processed/` folders, the
Drive token, the Syllabus account this Mac belongs to (`account.json`, if it
has been signed in), and `pipeline.log`. Set `INTAKE_HOME` to put the whole `~/.intake`
root somewhere else (`LECTUREAI_HOME`, the variable's old name, still works
when the new one is unset); the profile folders move with it. The code never
keeps anything next to itself.

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
and turns green as you go. Everything is saved on your Mac in `~/.intake/syllabus`;
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

That caches `token.json` in `~/.intake/syllabus`. The app uses the `drive.file`
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
that). The Sous panel is <http://127.0.0.1:5174>, so both can be open at once;
`--port` overrides either. One page to start and stop a recording,
start and stop the watcher, see what's waiting in the inbox, and open recent
lectures in Drive. It drives the same modules the CLI does, so a recording
started here is identical to one started with `intake record`.

The page is a dashboard, drawn entirely from `pipeline.log` and the
schedule, so nothing on it is stored anywhere else and a line removed from
the log disappears from it on the next poll:

- **This week** is the schedule as a grid, one column per day, one chip per
  class. A chip is filled once a recording for that class is filed (click
  it to open the Doc), ringed while the class is in session, dashed once
  the class has ended with nothing filed. The lecture's date comes from the
  filed name (`ACCT-4321_2026-09-10_...`), not from when it was processed,
  so a recording synced from a phone days later still lands on the right
  day.
- **Four tiles**: recorded this week against the classes that have met so
  far, lectures filed in all, the streak of consecutive classes recorded,
  and hours of audio filed. That last one is counted from a seventh field
  the watcher now writes to `pipeline.log` (seconds of audio, transcript
  words, to-dos and key terms, as JSON), so it reads as a dash until the
  next lecture is processed; the older lines have nothing to measure.
- **Lectures per week**, stacked by course for the last eight weeks, with a
  hairline per week at what the schedule expected; hover a week for the
  breakdown. **By course** beside it: how many lectures each course has and
  when its last one was. Clicking a course in either chart, or in the chips
  over the recent list, filters the recent list to that course.
- **Pipeline**: the watcher as a switch, the five stages as a stepper that
  lights up while a lecture is being processed, and what is waiting in the
  inbox.
- **Study assistant** at the bottom is reserved space for v3: a chat that
  answers questions and builds study guides from every transcript and
  summary in Drive. Nothing in it is wired up yet, and it says so.

Each course has a color, assigned in schedule order so it does not change
from week to week. The six colors were run through a colorblind-safety
check against both card surfaces, and identity never rests on color alone:
the code is always printed beside the mark.

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

`--host` can point it at another address, but only together with `--expose`,
and without that flag the panel refuses and says why: it has no login of its
own, so anyone who can reach the port can start processes and rewrite the
keys in `.env`. Reaching it from elsewhere is done differently, and without
that flag: the panel keeps listening on localhost, a Cloudflare Tunnel on the
same Mac carries requests to it, and the panel's own Google sign-in is the
login in front. That is the next section.

## Keeping the panel running

```bash
intake service install
```

Installs the panel as a launchd agent for your login: it starts now, starts
again at every login, and is restarted if it dies. It no longer belongs to
any terminal window, so closing one, or the app you launched it from, no
longer takes the panel down. Its output goes to `panel.log` in the profile's
home. `intake service status`, `restart` (after updating the code or
changing `.env`), and `uninstall` do what they say. Each profile has its
own agent (`sous service install` for the other one).

Only the panel is kept alive. Starting the watcher stays a click in the
panel, because the watcher spends API credit and writes to Drive and Notion.

The first recording after installing asks for microphone permission on
behalf of Python, the program the agent runs. Grant it once under System
Settings > Privacy & Security > Microphone and it sticks. The red timer
described above is what you see if it was refused.

## A Syllabus account

The Setup page ends with an optional step: **Sign in to a Syllabus account**.
An account is a Google identity kept by a small web service at
`syllabusaccounts.maincoursemedia.com` (its code is the
[syllabus-accounts](https://github.com/trace-J/syllabus-accounts) repo, a
Cloudflare Worker with a D1 database). Signing in ties this Mac's panel to
that identity. Today it does two things: the panel on the web lets in the
account's owner rather than a list of addresses in `.env` ("The panel on the
web" below), and it is the home for what comes next, when your class
schedule and your Drive connection belong to the account and follow you to
another Mac.

Signing in works the way a TV signs in to a streaming service. The panel
shows an eight-letter code and opens the account page in a new tab. Sign in
there with Google, enter the code, and the panel notices within a few
seconds. What it receives is a token that says "this Mac, this profile,
this account", saved as `account.json` in the profile's home with the same
permissions as your keys. The token is never sent to the browser, and your
Google session never reaches the panel. The account page lists every Mac on
the account and can remove one; the next time that Mac's Setup page loads,
it shows as signed out. **Sign out** on the Setup page does the same from
this end.

Once a Mac is signed in, **its class schedule belongs to the account too**.
Saving the Setup page pushes `schedule.toml` to the account, and starting
the panel pulls the account's copy when it is newer, so a second Mac signed
in to the same account gets the schedule without retyping it. The file on
this Mac stays what the pipeline reads; the account holds the text as
written, comments and all, with a version. When both sides changed since
they last agreed, the newer copy wins and the other is kept next to the
schedule as `schedule.toml.<stamp>.bak`. A copy from the account is parsed
before it replaces the file, so a broken schedule there cannot break this
Mac. The Setup page's account step says what the last sync did and when;
`.work/sync.json` remembers the version this Mac last agreed with.

**Google Drive can belong to the account as well.** On the Setup page,
with this Mac signed in, **Connect Google Drive through your account** opens
the account page, where Google's consent runs once for the `drive.file`
scope. The refresh token stays on the account service, encrypted; each Mac
asks it for an hour-long access token when a lecture is ready to upload.
Every Mac signed in to the account then files to the same Drive, and none of
them needs `intake login`. The uploader prefers the account's grant whenever
there is one and falls back to this Mac's own `token.json` otherwise, so a
Mac that already had Drive connected keeps working the day it signs in. If
the account service cannot be reached, the local token is used when there
is one; without one the upload fails with a message saying so and the
recording waits in the inbox for the retry. Disconnecting on the account
page revokes the grant at Google for every Mac at once.

Nothing requires an account. A panel with no `account.json` is exactly what
it was, and `intake doctor` reports the account as an optional line. To hide
the step altogether, put `ACCOUNTS_URL=off` in `.env`; to test against a
service of your own, point `ACCOUNTS_URL` at it. The dashboard's status poll
reads only the file, never the network; the Setup page asks the service to
confirm the token each time it loads.

## The panel on the web

The panel is published at `syllabus.maincoursemedia.com`, and
`maincoursemedia.com/syllabus` sends you there. Three pieces make that up,
and only the last needs anything from you when setting up a new Mac.

1. **The panel**, kept running by `intake service install`, listening on
   `127.0.0.1:5173` as always.
2. **A Cloudflare Tunnel** (`cloudflared`, installed with Homebrew and run
   as its own launch agent by `cloudflared service install <token>`). It
   holds an outbound connection to Cloudflare and hands requests for that
   hostname to the panel's port. Nothing is opened on the router, and the
   panel's port is still not reachable from the network. The tunnel is
   named `syllabus-panel` in the Cloudflare account; the hostname is a CNAME
   to `<tunnel-id>.cfargotunnel.com`.
3. **The sign-in.** Anyone who arrives through the tunnel has to sign in
   before they see anything, and who may enter depends on whether this
   Mac has been signed in to a Syllabus account ("A Syllabus account"
   above).

   **With an account**, the account's owner is the one person allowed.
   The browser is sent to the account service, signs in with Google there
   if it has not already, and comes back to the panel with a one-time code
   that the panel trades for the account using its own device token. A
   different account is told "That Syllabus belongs to someone else." The
   Web client and `PANEL_ALLOWED_EMAILS` are not consulted at all; the only
   setting the panel needs is where it is published:

   ```
   PANEL_PUBLIC_URL=https://syllabus.maincoursemedia.com
   ```

   **Without an account**, the older path still works: the panel runs
   Google's sign-in itself, and only the addresses named in `.env` may
   enter:

   ```
   PANEL_GOOGLE_CLIENT_ID=0123456789-abc.apps.googleusercontent.com
   PANEL_GOOGLE_CLIENT_SECRET=GOCSPX-...
   PANEL_ALLOWED_EMAILS=you@example.com, them@example.com
   PANEL_PUBLIC_URL=https://syllabus.maincoursemedia.com
   ```

   That client is a **Web application** OAuth client in the Google Cloud
   project named **LectureAI** (APIs & Services > Credentials > Create
   credentials > OAuth client ID). That is a different project from the one
   holding the bundled Desktop client `intake login` uses for Drive
   (friendly-bazaar-507320-b7); the two clients' ids start with their
   projects' numbers, which is how to tell them apart in the console. Its
   authorized redirect URIs are
   `https://syllabus.maincoursemedia.com/oauth2/callback` and, for the
   `panel-dev` preview, `http://127.0.0.1:5199/oauth2/callback`. The
   sign-in asks Google for nothing but the account's email; Drive access
   is a separate authorization and stays with `intake login`.

   `PANEL_PUBLIC_URL` is the address Google, or the account service, is
   told to come back to. It is needed because this tunnel's ingress
   rewrites the Host header to `127.0.0.1:5173` on the way in, so the panel
   cannot learn its public hostname from the request; without it the
   sign-in is asked to return to an address nobody has heard of, and Google
   answers "This app's request is invalid". Left empty, the request's own
   hostname is used, which is what the dev preview wants. Each `/login`
   writes the return address it used to `panel.log`, so a mismatch can be
   read straight off the log. On each account sign-in the panel also
   registers that address with the account service, which will only ever
   send a browser back there.

   Either way, a request that came through Cloudflare must carry a session
   from the sign-in or it is refused: the page is sent to `/login`, the API
   gets a 401. With neither an account nor the three settings, every request
   that came through Cloudflare is refused with a 503 saying so, so a tunnel
   that is up before the login is configured exposes nothing. Requests from
   the Mac itself carry no Cloudflare headers and are never gated, so
   `http://127.0.0.1:5173` keeps working whatever the tunnel is doing. The
   header shows who is signed in, with a sign-out link, when the page came
   through the tunnel.

   The session is a signed cookie, good for 30 days, keyed by a secret the
   panel generates once into `.work/panel-secret` (or `PANEL_SECRET_KEY` in
   `.env`, if you would rather manage it). A session issued through the
   account names the account, and stops counting the moment this Mac is
   signed out of it; one issued through the allowlist stops counting the
   moment the Mac is signed in to an account, or the address leaves the
   list. Signing in is done in `intake/signin.py`: the state of each
   attempt rides in a short-lived cookie, and on the Google path the ID
   token is checked with `google-auth` against Google's published keys and
   the email has to be verified and on the list.

Signing this Mac in to an account, from the Setup page, switches the web
sign-in over on the next request; signing it out switches back. Without an
account, adding a person is one more address in `PANEL_ALLOWED_EMAILS`,
followed by `intake service restart`, and removing one works the same way.

If the address stops working, check the pieces in order: `intake service
status` (is the panel up), `launchctl print gui/$(id -u)/com.cloudflare.cloudflared`
and `~/Library/Logs/com.cloudflare.cloudflared.err.log` (is the tunnel
connected), then `intake doctor`, whose "web sign-in" line says which way
the sign-in works and whether it is complete. A 503 page saying the sign-in
is not set up means this Mac has no account and `.env` is missing one of the
three settings; "belongs to someone else" means the Google account chosen is
not the one this Mac is signed in to; "has not told the account service where
it is published" means `PANEL_PUBLIC_URL` is empty or not https, so restart
the panel after fixing it; "not on the list" means the Google account chosen
is not in `PANEL_ALLOWED_EMAILS`; "could not be checked with Google" usually
means the redirect URI on the Web client does not match the hostname, or the
client secret is wrong. `panel.log` in the profile's home has the reason
each time.

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
`~/.intake/syllabus/inbox/`. The first run asks for microphone permission; if it
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
  `~/.intake/syllabus/.env`.
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
intake watch --once ~/.intake/syllabus/inbox/lecture.m4a
```

## The class schedule

`~/.intake/syllabus/schedule.toml` maps each class meeting to a course code. One
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
Record. ffmpeg's own log, if it said anything, is in `~/.intake/syllabus/.work/`.

**The panel says the watcher is running but nothing gets processed.** Restart
the panel; older versions judged the watcher by its pid, which a watcher that
had been killed kept answering until the panel that started it quit.

**You installed before profiles existed.** Your data sits flat in `~/.intake`
itself, where Syllabus no longer looks. The first `intake` or `syllabus`
command you run at a terminal offers to move your `.env`, `schedule.toml`,
Drive token, `.drive_root`, log, and any recordings in `inbox/` and
`processed/` down into `~/.intake/syllabus/`; say yes and restart any watcher
or panel that was already running. Only Syllabus is offered that data; Sous
starts empty. Until you say yes, `intake doctor` lists what is waiting under
"older install". Scratch in `~/.intake/.work/` and the old lock file stay put.

**You installed before the home directory existed.** Your `.env`, token, and
log are still next to the code. The first `intake` command you run offers
to move them into `~/.intake/syllabus`; say yes and restart any watcher or
panel that was already running.

**You installed when this was called lectureai.** Your data is in
`~/.lectureai`. The first `intake` command you run at a terminal offers to
move your `.env`, `schedule.toml`, Drive token, log, and any recordings in
`inbox/` and `processed/` into `~/.intake/syllabus`; the panel does not ask,
so use `intake setup` or `intake doctor` for that first run. Until you say yes,
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
   `~/.intake/syllabus/.env` yourself:

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
`~/.intake/syllabus/.env`:

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
command is one console script over it. `syllabus` and `sous` are the same
entry point with the profile chosen (`lectureai` is an older name for it,
kept for now). The profiles themselves are in `profiles.py`, one dataclass
each: home folder, schedule filename, summary schema and prompt (`schemas.py`),
Drive folder, filename prefix, Notion target, panel port. Every module reads
those from `config.PROFILE` rather than spelling them out. To work on it:

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
.venv/bin/python watch.py --once ~/.intake/syllabus/inbox/lecture.m4a
.venv/bin/python transcribe.py lecture.m4a > transcript.txt
.venv/bin/python summarize.py transcript.txt ACCT-4321 2026-09-03
.venv/bin/python upload.py summary.md ACCT-4321
.venv/bin/python gui.py
```

A dev checkout and a pipx install share `~/.intake` by default. To keep a
scratch install from touching your real recordings and keys, move the root:

```bash
INTAKE_HOME=/tmp/intake-scratch intake setup
```

That puts Syllabus in `/tmp/intake-scratch/syllabus/`. The root-level shims
(`python gui.py` and friends) run whichever profile `INTAKE_PROFILE` names,
Syllabus by default.

The Google OAuth client the app identifies itself with is `credentials.json`
inside the package, read by `google_client.py`. A `credentials.json` in the
home directory overrides it. The one at the repo root, if you have one from
before, stays gitignored.

That file holds a `client_secret`, and the repository is public, so it is
worth being exact about what that means. The client is a Google **Desktop**
client, and Google's own guidance is that a desktop client's secret is not
confidential: it ships inside every copy of the app and cannot be kept from
the person running it, which is why the flow does not rely on it for
security. What protects your Drive is the token in `~/.intake/syllabus`,
which never leaves your Mac, and the `drive.file` scope, which reaches only
files the app created. The exposure is that someone could put up their own
consent screen under this app's name; while the Cloud project is in Testing
only its listed test users can complete that screen at all. Never reuse this
client for anything server-side: a web app needs a **Web** client whose
secret stays on the server. Should its secret ever need resetting,
reset it on this same client in the Cloud console, put the new file in the
package, and the next release carries it; existing tokens keep working,
because they are tied to the client id, which does not change.

### Tests

Every suite is self-contained: the upload tests run against an in-memory fake
Drive, the recording tests open no microphone, the setup tests drive the
wizard with scripted answers into a temp directory. None needs network,
credentials, or an API key, none costs anything to run, and none can touch
`~/.intake`. They all run under the Syllabus profile whatever the shell says.

```bash
.venv/bin/python test_upload_collisions.py   # collisions, re-runs, two-part days
.venv/bin/python test_record.py              # device selection and naming
.venv/bin/python test_notion_tasks.py        # property mapping and de-duplication
.venv/bin/python test_gui.py                 # log parsing and API guard rails
.venv/bin/python test_insights.py            # the dashboard's numbers against a fake log and week
.venv/bin/python test_setup.py               # home directory, schedule file, setup wizard, doctor
.venv/bin/python test_profiles.py            # syllabus vs sous: homes, ports, folders, selection, migration
.venv/bin/python test_account.py             # claiming this Mac into a Syllabus account, against a fake service
.venv/bin/python test_sync.py                # the schedule file against the account's copy: push, pull, conflicts
```

## V2 roadmap

Everything described above is built and in daily use. These are what's left,
in rough order of how soon each one bites.

### New capability

- **Speaker diarization.** Separate the instructor from student questions.
- **Cross-lecture study guides.** Synthesize a whole unit rather than one
  lecture. The highest-value item for actually studying, and the one that most
  wants a database underneath it. The panel already holds the space for it:
  the "Study assistant" card at the bottom (v3) is where a chat over every
  transcript and summary will go, with study guides and quizzes as its
  starter prompts.
- **A database.** State currently lives in `pipeline.log` and the filesystem.

Dropped: **slide OCR**, decided against on 2026-09-08 as not worth the
complexity.

### The desktop app, and sign-ins

Syllabus is meant to become a real Mac app that other people can use, with an
account behind it. The web page at maincoursemedia.com/syllabus is the first
piece of that, and the rest is planned in this order:

- **Publish the Google Cloud project** (below). Done for the LectureAI
  project the account service uses; the Desktop client's project is still
  in Testing, which only matters for a Mac with no account.
- **A Mac app bundle.** The panel already is the app; what is missing is the
  wrapper. Plan: a `pywebview` window around the same Flask panel, packaged
  with PyInstaller into `Syllabus.app`, signed and notarized, with ffmpeg
  bundled so Homebrew stops being a requirement. The pipx install stays as
  the path for people who prefer a terminal.
- **Sign-ins.** Two pieces exist. This Mac's panel has its own Google
  sign-in in front of the tunnel ("The panel on the web" above), where a
  new person is one more email in `PANEL_ALLOWED_EMAILS`. And there is now
  an account service ("A Syllabus account" above): a Cloudflare Worker with
  D1, the same stack `mcm-dashboard` is scaffolded on, that owns the Google
  sign-in and lets a panel claim an identity with a device code, and the
  panel on the web now signs in through it, with the allowlist as the
  fallback for a Mac with no account; the class schedule syncs to it; and
  it can hold the Drive grant, handing each Mac short-lived access tokens.
  What is left, its own PR: `PANEL_ALLOWED_EMAILS` and the panel's own
  Google client retire once every published panel is on an account. That is also what would let Syllabus
  pay for transcription centrally instead of asking every student for two
  API keys.
- **The panel on the web is this Mac's panel.** It records from this Mac's
  microphone and starts processes here, so the address always reaches the
  one running on the Mac that does the recording. A second person's Syllabus
  would be a second tunnel to their Mac, under their own hostname.

### Loose ends

- **Publish the friendly-bazaar Cloud project.** The bundled Desktop
  client's project is still in Testing, so `intake login`, the path a Mac
  with no account takes, only works for accounts listed as test users and
  their tokens expire weekly. The LectureAI project, which the account
  service uses for sign-in and Drive, was published on 2026-09-13, so Drive
  through the account has neither limit.
- **Recover the September 3, 13:56 ACCT lecture.** Audio and transcript are
  gone, but the summary text survives in the raw JSON of the trashed Doc.
