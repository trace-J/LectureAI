"""Development entry point. The code lives in lectureai/notion_tasks.py.

Lets `python notion_tasks.py` from a checkout keep working alongside the installed
`lectureai` command, which is the same code behind a single name.
"""

from lectureai.notion_tasks import main

if __name__ == "__main__":
    raise SystemExit(main())
