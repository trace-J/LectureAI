"""Push lecture files to Google Drive, filed under a folder per course.

First run opens a browser for OAuth consent and caches the result in
token.json in the home directory; later runs are non-interactive. The OAuth
client itself ships with the package (see google_client.py).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from lectureai import config, google_client

FOLDER_MIME = "application/vnd.google-apps.folder"

# Drive infers a type from the extension, but guesses badly on .md.
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

MIME_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}

# Stamped on every uploaded file so a re-run can recognize its own past work.
# appProperties are private to this app and invisible in the Drive UI.
RECORDING_KEY_PROPERTY = "lectureRecordingStart"


class Upload(NamedTuple):
    """Where a file landed. `name` is the final name, which may differ from
    the one asked for if another recording already held it."""

    url: str
    name: str


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _escape(value: str) -> str:
    """Escape a value for a Drive query string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def get_credentials(interactive: bool = True) -> Credentials:
    """Load cached credentials, refreshing or running the OAuth flow as needed.

    With interactive=False, never opens a browser — a background watcher that
    pops a consent window nobody sees just hangs. Raises instead.
    """
    creds = None
    if config.TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(config.TOKEN_FILE), config.DRIVE_SCOPES
        )
        # A token minted under different scopes can't be refreshed into the
        # new ones — throw it away and re-consent instead of failing later.
        if creds and creds.scopes and set(creds.scopes) != set(config.DRIVE_SCOPES):
            log("  cached token has different scopes; re-authorizing")
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            config.TOKEN_FILE.write_text(creds.to_json())
            return creds
        except Exception as exc:
            log(f"  token refresh failed ({exc}); re-authorizing")

    if not interactive:
        raise RuntimeError(
            "Google authorization is needed but nothing can open a browser here. "
            "Run:  lectureai login"
        )

    log("  opening browser for Google authorization ...")
    flow = InstalledAppFlow.from_client_config(
        google_client.client_config(), config.DRIVE_SCOPES
    )
    creds = flow.run_local_server(port=0)
    config.TOKEN_FILE.write_text(creds.to_json())
    config.TOKEN_FILE.chmod(0o600)
    log(f"  authorized; token cached to {config.TOKEN_FILE.name}")
    return creds


def get_service(interactive: bool = True):
    """An authenticated Drive v3 client."""
    return build("drive", "v3", credentials=get_credentials(interactive),
                 cache_discovery=False)


def _folder_exists(service, folder_id: str) -> bool:
    try:
        meta = service.files().get(
            fileId=folder_id, fields="id, trashed", supportsAllDrives=True
        ).execute()
        return not meta.get("trashed", False)
    except HttpError as exc:
        if exc.status_code in (403, 404):
            return False
        raise


def ensure_root_folder(service) -> str:
    """Id of the app's root folder, creating it on first use.

    Order: an explicit DRIVE_PARENT_FOLDER_ID override, then the cached id,
    then a search by name, then create. The cache means renaming or moving the
    folder in Drive doesn't strand the pipeline.
    """
    if config.DRIVE_PARENT_FOLDER_ID:
        return config.DRIVE_PARENT_FOLDER_ID

    if config.DRIVE_ROOT_CACHE.exists():
        cached = config.DRIVE_ROOT_CACHE.read_text().strip()
        if cached and _folder_exists(service, cached):
            return cached

    name = config.DRIVE_ROOT_FOLDER_NAME
    # Under drive.file this only sees folders this app created, so a folder of
    # the same name elsewhere in your Drive won't be matched by accident.
    found = service.files().list(
        q=f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false",
        fields="files(id, name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])

    if found:
        folder_id = found[0]["id"]
    else:
        log(f"  creating Drive root folder '{name}' in My Drive")
        folder_id = service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME},
            fields="id", supportsAllDrives=True,
        ).execute()["id"]

    config.DRIVE_ROOT_CACHE.write_text(folder_id)
    return folder_id


def ensure_folder(service, name: str, parent_id: str) -> str:
    """Return the id of `name` under `parent_id`, creating it if absent."""
    query = (
        f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}' "
        f"and '{_escape(parent_id)}' in parents and trashed = false"
    )
    found = service.files().list(
        q=query, fields="files(id, name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])

    if found:
        return found[0]["id"]

    log(f"  creating Drive folder '{name}'")
    folder = service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id", supportsAllDrives=True,
    ).execute()
    return folder["id"]


def _find_file(service, name: str, folder_id: str) -> dict | None:
    """An existing file called `name` in `folder_id`, with its appProperties."""
    query = (
        f"name = '{_escape(name)}' and '{_escape(folder_id)}' in parents "
        f"and trashed = false"
    )
    found = service.files().list(
        q=query, fields="files(id, appProperties)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    return found[0] if found else None


def _find_by_recording(service, key: str, folder_id: str) -> dict | None:
    """The file in `folder_id` that a previous run of this recording produced."""
    query = (
        f"appProperties has {{ key='{RECORDING_KEY_PROPERTY}' "
        f"and value='{_escape(key)}' }} "
        f"and '{_escape(folder_id)}' in parents and trashed = false"
    )
    found = service.files().list(
        q=query, fields="files(id, name)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    return found[0] if found else None


def _add_suffix(name: str, suffix: str) -> str:
    """Insert `suffix` before the extension: X.txt + 1447 -> X_1447.txt.

    Google Docs are stored without an extension, so a name with no dot just
    gets the suffix appended.
    """
    ext = Path(name).suffix
    return f"{name[:len(name) - len(ext)] if ext else name}_{suffix}{ext}"


def _free_name(service, name: str, folder_id: str, suffix: str) -> str:
    """A name in `folder_id` that no *other* recording has already taken.

    Tries the time-of-day suffix first, since that reads as what it is: the
    second recording of a class the same day. Falls back to counting only if
    that is somehow taken too.
    """
    candidates = [_add_suffix(name, suffix)] if suffix else []
    candidates += [_add_suffix(name, f"{n}") for n in range(2, 20)]
    for candidate in candidates:
        if _find_file(service, candidate, folder_id) is None:
            return candidate
    raise RuntimeError(f"could not find a free name for {name} after 20 tries")


def upload(
    local_path: str | Path,
    course: str,
    interactive: bool = True,
    *,
    subfolder: str = "",
    as_google_doc: bool = False,
    name: str | None = None,
    recording_key: str | None = None,
    time_suffix: str = "",
) -> Upload:
    """Upload a file into the course's Drive folder. Returns the link and name.

    subfolder      files into course/<subfolder>/ instead of course/
    as_google_doc  asks Drive to convert the upload into a native Google Doc
    name           overrides the Drive filename (a Doc wants no extension)
    recording_key  config.recording_key() for the source recording
    time_suffix    config.recording_time_suffix(), used to break a collision

    Replaces a file only when it can prove the file came from this same
    recording, matched on `recording_key` rather than on the filename. Two
    recordings of one class on one day produce the same
    COURSE_DATE_Topic-Slug, and overwriting on a name match silently destroyed
    the first one. Anything that can't be proven to be the same recording gets
    a name of its own, because a duplicate in Drive is cheap and a lost lecture
    is not.

    Without a recording_key it falls back to matching on name alone, which is
    the old behavior and is fine for one-off CLI uploads.
    """
    path = Path(local_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"no such file: {path}")

    service = get_service(interactive)
    folder_id = ensure_folder(service, course, ensure_root_folder(service))
    where = course
    if subfolder:
        folder_id = ensure_folder(service, subfolder, folder_id)
        where = f"{course}/{subfolder}"

    drive_name = name or path.name
    mime = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
    media = MediaFileUpload(str(path), mimetype=mime, resumable=True)

    replacing = None

    # A previous run of this same recording, wherever its slug happened to
    # land. Found by key, so a changed topic slug still resolves to one file.
    if recording_key:
        prior = _find_by_recording(service, recording_key, folder_id)
        if prior:
            replacing = prior["id"]
            if prior.get("name") != drive_name:
                log(f"  same recording, new name: {prior.get('name')} -> {drive_name}")

    if replacing is None:
        clash = _find_file(service, drive_name, folder_id)
        if clash:
            clash_key = (clash.get("appProperties") or {}).get(RECORDING_KEY_PROPERTY)
            if not recording_key or clash_key is None or clash_key == recording_key:
                # No key on either side means a file from before keys existed,
                # or a plain CLI upload. Treat the name as the identity, as
                # this always used to.
                replacing = clash["id"]
            else:
                taken = drive_name
                drive_name = _free_name(service, drive_name, folder_id, time_suffix)
                log(f"  {taken} belongs to another recording; "
                    f"filing this one as {drive_name}")

    body: dict = {"name": drive_name}
    if recording_key:
        body["appProperties"] = {RECORDING_KEY_PROPERTY: recording_key}

    if replacing:
        log(f"  replacing {drive_name} in {where}/")
        result = service.files().update(
            fileId=replacing, body=body, media_body=media,
            fields="id, webViewLink", supportsAllDrives=True,
        ).execute()
    else:
        kind = "Google Doc" if as_google_doc else path.suffix.lstrip(".")
        log(f"  uploading {drive_name} to {where}/ as {kind}")
        body["parents"] = [folder_id]
        if as_google_doc:
            # Drive converts the uploaded markdown into a native Doc.
            body["mimeType"] = GOOGLE_DOC_MIME
        result = service.files().create(
            body=body, media_body=media,
            fields="id, webViewLink", supportsAllDrives=True,
        ).execute()

    return Upload(result["webViewLink"], drive_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload a file to its course folder in Google Drive."
    )
    parser.add_argument("file", nargs="?", help="path to the file to upload")
    parser.add_argument("course", nargs="?", help="course code, e.g. ACCT-4321")
    parser.add_argument("--login", action="store_true",
                        help="run the OAuth flow and exit, without uploading")
    args = parser.parse_args(argv)

    try:
        if args.login:
            get_credentials()
            log("authorization complete")
            return 0
        if not args.file or not args.course:
            parser.error("file and course are required unless --login is given")
        print(upload(args.file, args.course).url)
    except HttpError as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        log(f"error: Drive API returned {exc.status_code}: {detail}")
        if exc.status_code == 404:
            log(f"  if you set DRIVE_PARENT_FOLDER_ID, the drive.file scope can "
                f"only reach folders this app created; unset it and let the app "
                f"make its own, or delete {config.DRIVE_ROOT_CACHE.name} to reset")
        return 1
    except Exception as exc:
        log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
