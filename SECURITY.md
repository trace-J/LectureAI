# Security policy

Syllabus records audio, holds a Google token that can write to your Drive, and
keeps API keys for the services that transcribe and summarize. A flaw here can
leak a recording or a credential, so reports are genuinely welcome.

## Reporting a vulnerability

Use GitHub's private reporting:
[open an advisory](https://github.com/SyllabusAI/LectureAI/security/advisories/new)
and describe what you found. The report stays between us until there is a fix.
Please do not open a public issue or pull request for a security problem, and
please do not test against anyone else's account or recordings.

This is a one-person project with no bug bounty and no paid on-call. Expect a
first reply within about a week. If you believe something is being exploited
right now, say so in the title and it moves to the front.

A useful report carries the version (`intake --version`, or the version shown
in the panel), the macOS version, what an attacker ends up able to reach, and
the shortest steps that demonstrate it.

## Versions that get fixes

The newest release on the
[releases page](https://github.com/SyllabusAI/LectureAI/releases) and whatever is
currently on `main`. There are no backports to older versions; a fix ships in
the next release.

## In scope

- The recording, transcription, summary, and upload pipeline in this
  repository, under either profile (Syllabus or Sous).
- The local control panel, including how it decides a browser is allowed in.
- The packaged `Syllabus.app` and the disk image it ships in.
- How the Google OAuth token, the service API keys, and the recordings
  themselves are stored on disk and cleaned up.

## Out of scope

- **The OAuth client in `intake/credentials.json`.** It is committed on
  purpose. A desktop OAuth client secret is not a secret, Google documents it
  as such, and every user still authorizes their own account and holds their
  own token. A report that it is "exposed" will be closed.
- **The account service.** Sign-in, device claim, and the web relay live in
  [syllabus-accounts](https://github.com/SyllabusAI/syllabus-accounts) and have
  their own policy. Report those there.
- **Bugs in the providers themselves**, meaning Google, OpenAI, Anthropic,
  Deepgram, Notion, or ffmpeg. Those go upstream.
- Anything that assumes the attacker already has your unlocked Mac, your
  Google account, or your `~/.intake` directory.
