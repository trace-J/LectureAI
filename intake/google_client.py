"""The Google OAuth client the app identifies itself with.

Google treats a Desktop-app client's secret as non-confidential: the consent
screen, not the secret, is what protects the user, and every copy of a desktop
app necessarily carries the same one. So the client config ships inside the
package (credentials.json next to this file, declared as package data) and
nobody installing LectureAI has to create a Cloud project of their own.

A credentials.json in the home directory overrides the bundled one, for the
developer who wants to test against a different Cloud project.

The bundled client's Cloud project (friendly-bazaar-507320-b7) has been In
production since 2026-09-14, so anyone can complete `intake login` and the
refresh tokens no longer expire weekly. The Syllabus account service's Web
client lives in the same project, which is what lets a Drive grant on the
account and a Mac's own token see the same files.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from intake import config

BUNDLED_NAME = "credentials.json"


def bundled_client_config() -> dict:
    """The client config shipped inside the package, as the Google libraries
    expect it: {"installed": {"client_id": ..., "client_secret": ..., ...}}."""
    text = resources.files("intake").joinpath(BUNDLED_NAME).read_text()
    return json.loads(text)


def override_path() -> Path | None:
    """A per-developer credentials.json in the home directory, if there is one."""
    return config.CREDENTIALS_FILE if config.CREDENTIALS_FILE.exists() else None


def client_config() -> dict:
    """The client config to authorize with: the home override, else bundled."""
    override = override_path()
    if override is not None:
        return json.loads(override.read_text())
    return bundled_client_config()


def describe() -> str:
    """One line saying which client is in use, for `intake doctor`."""
    override = override_path()
    if override is not None:
        return f"override at {override}"
    installed = bundled_client_config().get("installed", {})
    project = installed.get("project_id", "unknown project")
    return f"bundled with the package (Cloud project {project})"
