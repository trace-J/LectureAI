"""Development entry point. The code lives in intake/transcribe.py.

Lets `python transcribe.py` from a checkout keep working alongside the installed
`intake` command, which is the same code behind a single name.
"""

from intake.transcribe import main

if __name__ == "__main__":
    raise SystemExit(main())
