"""Upload process output to Google Drive."""

from __future__ import annotations

import json
from typing import Any

from evalforge_runtime.actions.base import BaseAction


class GDriveUploadAction(BaseAction):
    type = "gdrive.upload"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], **kwargs: Any) -> None:
        folder_id = self.config.get("folderId", "")
        filename = self.render_template(self.config.get("filename", "output"), output)
        fmt = self.config.get("format", "json")

        if not folder_id:
            raise ValueError("Google Drive folder ID is required")

        sa_key = secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if not sa_key:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON secret is required")

        # Prepare content
        if fmt == "csv":
            import csv
            import io
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(output.keys()))
            writer.writeheader()
            writer.writerow(output)
            content = buf.getvalue().encode("utf-8")
            mime_type = "text/csv"
            ext = "csv"
        else:
            content = json.dumps(output, indent=2, ensure_ascii=False).encode("utf-8")
            mime_type = "application/json"
            ext = "json"

        full_filename = f"{filename}.{ext}"

        # Upload via Google Drive API
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaInMemoryUpload

        creds_info = json.loads(sa_key)
        creds = service_account.Credentials.from_service_account_info(
            creds_info, scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        service = build("drive", "v3", credentials=creds)

        file_metadata = {
            "name": full_filename,
            "parents": [folder_id],
        }
        media = MediaInMemoryUpload(content, mimetype=mime_type)
        service.files().create(
            body=file_metadata, media_body=media, fields="id"
        ).execute()
