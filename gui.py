"""Development entry point. The code lives in intake/gui.py.

Lets `python gui.py` from a checkout keep working alongside the installed
`intake` command, which is the same code behind a single name.
"""

from intake.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
