"""Development entry point. The code lives in intake/record.py.

Lets `python record.py` from a checkout keep working alongside the installed
`intake` command, which is the same code behind a single name.
"""

from intake.record import main

if __name__ == "__main__":
    raise SystemExit(main())
