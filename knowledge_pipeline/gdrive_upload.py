"""
knowledge_pipeline/gdrive_upload.py — Upload markdown chunks to Google Drive
Uses a service account (no browser auth, no sessions to expire).

Setup (one-time):
  1. Create a service account at console.cloud.google.com
  2. Enable Google Drive API for the project
  3. Download the JSON key → save as `google-service-account.json` in project root
     OR set GDRIVE_SERVICE_ACCOUNT_JSON=/path/to/key.json in .env
  4. Set GDRIVE_KNOWLEDGE_FOLDER_ID=<Drive folder ID> in .env (optional)
     — if omitted, files are created in the service account's root Drive
"""

import os
from pathlib import Path
from typing import Optional

_SA_ENV_VAR      = "GDRIVE_SERVICE_ACCOUNT_JSON"
_FOLDER_ENV_VAR  = "GDRIVE_KNOWLEDGE_FOLDER_ID"
_SA_DEFAULT_FILE = Path(__file__).parent.parent / "google-service-account.json"

_drive_service = None


# ── Service account auth ──────────────────────────────────────────────────────

def _get_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service

    try:
        from googleapiclient.discovery import build
        from google.oauth2.service_account import Credentials
    except ImportError:
        raise ImportError(
            "Run: pip install google-api-python-client google-auth"
        )

    sa_path = os.getenv(_SA_ENV_VAR)
    if sa_path and Path(sa_path).exists():
        cred_file = sa_path
    elif _SA_DEFAULT_FILE.exists():
        cred_file = str(_SA_DEFAULT_FILE)
    else:
        raise FileNotFoundError(
            "Google service account key not found.\n"
            f"  Option A: Set {_SA_ENV_VAR}=/path/to/key.json in .env\n"
            f"  Option B: Place key file at {_SA_DEFAULT_FILE}\n"
            "  See: https://cloud.google.com/iam/docs/creating-managing-service-account-keys"
        )

    creds = Credentials.from_service_account_file(
        cred_file,
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def _get_or_create_folder(service, name: str, parent_id: Optional[str] = None) -> str:
    q = (
        f"name='{name}' "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"

    resp  = service.files().list(q=q, fields="files(id)").execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]

    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


# ── Public API ────────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True if service account credentials are available."""
    sa_path = os.getenv(_SA_ENV_VAR)
    return bool(
        (sa_path and Path(sa_path).exists()) or
        _SA_DEFAULT_FILE.exists()
    )


def upload_files(
    files:             list[Path],
    specialty:         str,
    doc_type:          str,
    root_folder_id:    Optional[str] = None,
    progress_callback  = None,
) -> list[str]:
    """
    Upload markdown files to:
      CaseFlow KnowledgeBase/{specialty}/{doc_type}/

    Returns list of Drive file IDs (empty list on failure / not configured).
    """
    if not is_configured():
        print("[gdrive] service account not configured — skipping GDrive upload")
        print(f"[gdrive] files saved locally in knowledge_base/{specialty}/{doc_type}/")
        return []

    if not root_folder_id:
        root_folder_id = os.getenv(_FOLDER_ENV_VAR)

    try:
        service = _get_service()
        from googleapiclient.http import MediaFileUpload
    except (ImportError, FileNotFoundError) as e:
        print(f"[gdrive] {e}")
        return []

    # Build folder hierarchy
    root_id   = _get_or_create_folder(service, "CaseFlow KnowledgeBase", root_folder_id or None)
    spec_id   = _get_or_create_folder(service, specialty.replace("_", " ").title(), root_id)
    type_id   = _get_or_create_folder(service, doc_type, spec_id)

    uploaded_ids: list[str] = []
    total = len(files)

    for i, fpath in enumerate(files, 1):
        try:
            file_meta = {"name": fpath.name, "parents": [type_id]}
            media     = MediaFileUpload(str(fpath), mimetype="text/markdown", resumable=False)
            result    = service.files().create(
                body=file_meta, media_body=media, fields="id"
            ).execute()
            uploaded_ids.append(result["id"])

            if progress_callback:
                progress_callback(
                    f"อัพโหลด GDrive [{specialty}]", "running",
                    f"{i}/{total}", current=i, total=total,
                )
        except Exception as e:
            print(f"[gdrive] upload failed for {fpath.name}: {e}")

    if progress_callback:
        ok = len(uploaded_ids)
        fail = total - ok
        progress_callback(
            f"อัพโหลด GDrive [{specialty}]", "done",
            f"{ok} ok / {fail} fail",
        )

    return uploaded_ids
