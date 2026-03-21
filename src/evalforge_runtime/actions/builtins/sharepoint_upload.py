"""Upload process output to SharePoint / OneDrive."""

from __future__ import annotations

import json
from typing import Any

import httpx

from evalforge_runtime.actions.base import BaseAction


class SharePointUploadAction(BaseAction):
    type = "sharepoint.upload"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], **kwargs: Any) -> None:
        site_id = self.config.get("siteId", "")
        drive_path = self.config.get("drivePath", "/General/Results")
        filename = self.render_template(self.config.get("filename", "output"), output)
        fmt = self.config.get("format", "json")

        if not site_id:
            raise ValueError("SharePoint site ID is required")

        tenant_id = secrets.get("SHAREPOINT_TENANT_ID", "")
        client_id = secrets.get("SHAREPOINT_CLIENT_ID", "")
        client_secret = secrets.get("SHAREPOINT_CLIENT_SECRET", "")

        if not all([tenant_id, client_id, client_secret]):
            raise ValueError("SharePoint secrets (TENANT_ID, CLIENT_ID, CLIENT_SECRET) are required")

        # Prepare content
        if fmt == "csv":
            import csv
            import io
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(output.keys()))
            writer.writeheader()
            writer.writerow(output)
            content = buf.getvalue().encode("utf-8")
            ext = "csv"
        else:
            content = json.dumps(output, indent=2, ensure_ascii=False).encode("utf-8")
            ext = "json"

        full_filename = f"{filename}.{ext}"

        # Acquire OAuth2 token
        token = await self._acquire_token(tenant_id, client_id, client_secret)

        # Upload via Microsoft Graph API
        # PUT /sites/{siteId}/drive/root:/{path}/{filename}:/content
        upload_path = f"{drive_path.rstrip('/')}/{full_filename}"
        url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root:{upload_path}:/content"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(
                url,
                content=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                },
            )
            response.raise_for_status()

    async def _acquire_token(
        self, tenant_id: str, client_id: str, client_secret: str
    ) -> str:
        """Acquire OAuth2 token via client credentials flow."""
        url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            response.raise_for_status()
            return response.json()["access_token"]
