"""Development entry point. The code lives in intake/notion_tasks.py.

Lets `python notion_tasks.py` from a checkout keep working alongside the installed
`intake` command, which is the same code behind a single name.
"""

from intake.notion_tasks import main

if __name__ == "__main__":
    raise SystemExit(main())
