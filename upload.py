"""Development entry point. The code lives in intake/upload.py.

Lets `python upload.py` from a checkout keep working alongside the installed
`intake` command, which is the same code behind a single name.
"""

from intake.upload import main

if __name__ == "__main__":
    raise SystemExit(main())
