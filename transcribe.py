"""Development entry point. The code lives in lectureai/transcribe.py.

Lets `python transcribe.py` from a checkout keep working alongside the installed
`lectureai` command, which is the same code behind a single name.
"""

from lectureai.transcribe import main

if __name__ == "__main__":
    raise SystemExit(main())
