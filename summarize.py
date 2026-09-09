"""Development entry point. The code lives in lectureai/summarize.py.

Lets `python summarize.py` from a checkout keep working alongside the installed
`lectureai` command, which is the same code behind a single name.
"""

from lectureai.summarize import main

if __name__ == "__main__":
    raise SystemExit(main())
