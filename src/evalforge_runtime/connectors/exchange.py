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
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from evalforge_runtime.connectors.base import Connector, ConnectorItem
from evalforge_runtime.storage import LocalStorage

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
        self._max_age_minutes = int(
            self.params.get("maxAgeMinutes", os.environ.get("EMAIL_MAX_AGE_MINUTES", "1440"))
        )

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

        # Build OData filter with date constraint
        since = (datetime.now(timezone.utc) - timedelta(minutes=self._max_age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters: list[str] = [f"receivedDateTime ge {since}"]
        if self._filter == "unread":
            filters.append("isRead eq false")
        filter_str = "$filter=" + " and ".join(filters)
        logger.debug("Exchange fetch filter: %s", filter_str)

        url = (
            f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
            f"/mailFolders/{self._folder}/messages?{filter_str}&$top=50"
        )

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            messages = resp.json().get("value", [])

        items: list[ConnectorItem] = []
        async with httpx.AsyncClient() as client:
            for msg in messages:
                data: dict[str, Any] = {
                    "email_subject": msg.get("subject", ""),
                    "email_body": msg.get("body", {}).get("content", ""),
                    "sender": msg.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "received_at": msg.get("receivedDateTime", ""),
                    "has_attachments": msg.get("hasAttachments", False),
                }

                # Download raw MIME as .eml file (skip if already saved)
                if self.storage:
                    try:
                        subject_slug = _slugify(msg.get("subject", "email"))[:60]
                        eml_filename = f"{subject_slug}.eml"
                        eml_key = f"connector/exchange/{msg['id']}/{eml_filename}"
                        if not await self.storage.exists(eml_key):
                            mime_resp = await client.get(
                                f"https://graph.microsoft.com/v1.0/users/{self._mailbox}"
                                f"/messages/{msg['id']}/$value",
                                headers=headers,
                            )
                            mime_resp.raise_for_status()
                            raw_bytes = mime_resp.content
                            await self.storage.put(eml_key, raw_bytes, "message/rfc822")
                            eml_size = len(raw_bytes)
                            logger.info("Saved .eml file: %s (%d bytes)", eml_key, eml_size)
                        else:
                            eml_size = await self.storage.size(eml_key)
                        data["file"] = {
                            "type": "local",
                            "key": eml_key,
                            "filename": eml_filename,
                            "size": eml_size,
                            "mimeType": "message/rfc822",
                            "extension": "eml",
                        }
                    except Exception as e:
                        logger.warning("Failed to download .eml for message %s: %s", msg["id"], e)

                items.append(ConnectorItem(ref=msg["id"], data=data))

        return items

    # --- Output methods (used by after steps) ---

    async def send_message(
        self, to: list[str], subject: str, body: str,
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


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "email"
