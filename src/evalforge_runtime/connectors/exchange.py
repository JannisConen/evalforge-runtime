"""Microsoft Exchange Online connector via Microsoft Graph API.

Supports shared mailboxes — the service principal needs
Mail.ReadWrite application permission on the target mailbox.

Required secrets:
  EXCHANGE_TENANT_ID
  EXCHANGE_CLIENT_ID
  EXCHANGE_CLIENT_SECRET
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from evalforge_runtime.connectors.base import Connector, ConnectorItem
from evalforge_runtime.storage import LocalStorage
from evalforge_runtime.types import FileRef

logger = logging.getLogger(__name__)


class ExchangeConnector(Connector):
    """Microsoft Exchange connector via Graph API."""

    def __init__(
        self,
        params: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        storage: LocalStorage | None = None,
    ):
        super().__init__(params, secrets)
        self.storage = storage
        self._token: str | None = None
        self._mailbox = self.params.get("mailbox", "")
        self._folder = self.params.get("folder", "Inbox")
        self._filter = self.params.get("filter", "unread")

    def name(self) -> str:
        return "exchange-inbox"

    async def validate(self) -> None:
        """Validate Exchange credentials by acquiring a token."""
        required = ["EXCHANGE_TENANT_ID", "EXCHANGE_CLIENT_ID", "EXCHANGE_CLIENT_SECRET"]
        missing = [k for k in required if k not in self.secrets]
        if missing:
            raise ValueError(f"Exchange connector missing secrets: {', '.join(missing)}")
        if not self._mailbox:
            raise ValueError("Exchange connector requires 'mailbox' parameter")
        await self._acquire_token()

    async def _acquire_token(self) -> str:
        """Acquire OAuth2 token via client credentials flow."""
        if self._token:
            return self._token

        tenant_id = self.secrets["EXCHANGE_TENANT_ID"]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.secrets["EXCHANGE_CLIENT_ID"],
                    "client_secret": self.secrets["EXCHANGE_CLIENT_SECRET"],
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
            return self._token

    async def fetch(self) -> list[ConnectorItem]:
        """Fetch unread messages from the configured mailbox."""
        token = await self._acquire_token()
        headers = {"Authorization": f"Bearer {token}"}

        filter_str = "$filter=isRead eq false" if self._filter == "unread" else ""
        url = (
            f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
            f"/mailFolders/{self._folder}/messages?{filter_str}&$top=50"
        )

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            messages = resp.json().get("value", [])

        items: list[ConnectorItem] = []
        for msg in messages:
            attachments: list[FileRef] = []
            if msg.get("hasAttachments") and self.storage:
                attachments = await self._fetch_attachments(
                    msg["id"], headers, msg["id"]
                )

            items.append(ConnectorItem(
                ref=msg["id"],
                data={
                    "email_subject": msg.get("subject", ""),
                    "email_body": msg.get("body", {}).get("content", ""),
                    "sender": msg.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "received_at": msg.get("receivedDateTime", ""),
                },
                attachments=attachments,
            ))

        return items

    async def _fetch_attachments(
        self, message_id: str, headers: dict, execution_ref: str
    ) -> list[FileRef]:
        """Fetch and store attachments for a message."""
        if not self.storage:
            return []

        url = (
            f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
            f"/messages/{message_id}/attachments"
        )

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            attachments_data = resp.json().get("value", [])

        refs: list[FileRef] = []
        for att in attachments_data:
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue

            import base64
            content = base64.b64decode(att.get("contentBytes", ""))
            filename = att.get("name", "attachment")
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
            mime_type = att.get("contentType", "application/octet-stream")
            key = f"connector/exchange/{execution_ref}/{filename}"

            await self.storage.put(key, content, mime_type)

            refs.append(FileRef(
                type="local",
                key=key,
                filename=filename,
                size=len(content),
                mimeType=mime_type,
                extension=extension,
            ))

        return refs

    # --- Output methods (used by after steps) ---

    async def send_message(
        self, to: list[str], subject: str, body: str,
        attachments: list[FileRef] | None = None,
    ) -> str:
        """Send a new email. Returns message ID."""
        token = await self._acquire_token()
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}/sendMail",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"message": message, "saveToSentItems": True},
            )
            resp.raise_for_status()
        return "sent"

    async def reply(self, message_id: str, body: str, reply_all: bool = False) -> None:
        """Reply to an existing message."""
        token = await self._acquire_token()
        endpoint = "replyAll" if reply_all else "reply"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
                f"/messages/{message_id}/{endpoint}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"comment": body},
            )
            resp.raise_for_status()

    async def forward(self, message_id: str, to: str, comment: str = "") -> None:
        """Forward a message."""
        token = await self._acquire_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
                f"/messages/{message_id}/forward",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "comment": comment,
                    "toRecipients": [{"emailAddress": {"address": to}}],
                },
            )
            resp.raise_for_status()

    async def move_message(self, message_id: str, folder: str) -> None:
        """Move a message to a folder."""
        token = await self._acquire_token()

        async with httpx.AsyncClient() as client:
            # Get or create folder
            folder_id = await self._get_or_create_folder(folder, token)
            resp = await client.post(
                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
                f"/messages/{message_id}/move",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"destinationId": folder_id},
            )
            resp.raise_for_status()

    async def mark_read(self, message_id: str) -> None:
        """Mark a message as read."""
        token = await self._acquire_token()

        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
                f"/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"isRead": True},
            )
            resp.raise_for_status()

    async def _get_or_create_folder(self, folder_name: str, token: str) -> str:
        """Get folder ID by name, creating it if needed."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}/mailFolders"
                f"?$filter=displayName eq '{folder_name}'",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            folders = resp.json().get("value", [])
            if folders:
                return folders[0]["id"]

            # Create folder
            resp = await client.post(
                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}/mailFolders",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"displayName": folder_name},
            )
            resp.raise_for_status()
            return resp.json()["id"]
