"""Gmail / Google Workspace connector via Gmail API.

Supports shared mailboxes via domain-wide delegation with a service account.

Required secrets:
  GOOGLE_SERVICE_ACCOUNT_JSON — service account key (JSON string)
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from evalforge_runtime.connectors.base import Connector, ConnectorItem
from evalforge_runtime.storage import LocalStorage
from evalforge_runtime.types import FileRef

logger = logging.getLogger(__name__)


class GmailConnector(Connector):
    """Gmail connector via Gmail API with service account auth."""

    def __init__(
        self,
        params: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        storage: LocalStorage | None = None,
    ):
        super().__init__(params, secrets)
        self.storage = storage
        self._token: str | None = None
        self._mailbox = self.params.get("mailbox", "me")
        self._filter = self.params.get("filter", "is:unread")

    def name(self) -> str:
        return "gmail-inbox"

    async def validate(self) -> None:
        """Validate Gmail credentials."""
        if "GOOGLE_SERVICE_ACCOUNT_JSON" not in self.secrets:
            raise ValueError("Gmail connector requires GOOGLE_SERVICE_ACCOUNT_JSON secret")
        if not self._mailbox:
            raise ValueError("Gmail connector requires 'mailbox' parameter")

    async def _acquire_token(self) -> str:
        """Acquire OAuth2 token via service account JWT flow."""
        if self._token:
            return self._token

        # In production, use google-auth library for proper JWT signing
        # This is a simplified implementation
        sa_info = json.loads(self.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])

        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request as GoogleRequest

            credentials = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
                subject=self._mailbox,
            )
            credentials.refresh(GoogleRequest())
            self._token = credentials.token
        except ImportError:
            raise RuntimeError(
                "Gmail connector requires google-auth package. "
                "Install with: pip install google-auth"
            )

        return self._token

    async def fetch(self) -> list[ConnectorItem]:
        """Fetch messages matching the filter."""
        token = await self._acquire_token()
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}/messages",
                headers=headers,
                params={"q": self._filter, "maxResults": 50},
            )
            resp.raise_for_status()
            message_ids = [m["id"] for m in resp.json().get("messages", [])]

        items: list[ConnectorItem] = []
        async with httpx.AsyncClient() as client:
            for msg_id in message_ids:
                resp = await client.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}"
                    f"/messages/{msg_id}?format=full",
                    headers=headers,
                )
                resp.raise_for_status()
                msg = resp.json()

                headers_map = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }

                items.append(ConnectorItem(
                    ref=msg_id,
                    data={
                        "email_subject": headers_map.get("subject", ""),
                        "email_body": self._extract_body(msg.get("payload", {})),
                        "sender": headers_map.get("from", ""),
                        "received_at": headers_map.get("date", ""),
                    },
                ))

        return items

    def _extract_body(self, payload: dict) -> str:
        """Extract text body from Gmail message payload."""
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

        for part in payload.get("parts", []):
            body = self._extract_body(part)
            if body:
                return body

        return ""

    # --- Output methods ---

    async def send_message(
        self, to: list[str], subject: str, body: str,
        attachments: list[FileRef] | None = None,
    ) -> str:
        """Send a new email."""
        token = await self._acquire_token()
        import email.mime.text

        msg = email.mime.text.MIMEText(body, "html")
        msg["to"] = ", ".join(to)
        msg["subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}/messages/send",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"raw": raw},
            )
            resp.raise_for_status()
            return resp.json().get("id", "sent")

    async def reply(self, message_id: str, body: str, reply_all: bool = False) -> None:
        """Reply to a message."""
        token = await self._acquire_token()

        # Get original message for threading
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}"
                f"/messages/{message_id}?format=metadata",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            msg = resp.json()

        thread_id = msg.get("threadId")
        headers_map = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        import email.mime.text
        reply_msg = email.mime.text.MIMEText(body, "html")
        reply_msg["to"] = headers_map.get("from", "")
        reply_msg["subject"] = "Re: " + headers_map.get("subject", "")
        reply_msg["In-Reply-To"] = headers_map.get("message-id", "")
        reply_msg["References"] = headers_map.get("message-id", "")

        raw = base64.urlsafe_b64encode(reply_msg.as_bytes()).decode("utf-8")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}/messages/send",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"raw": raw, "threadId": thread_id},
            )
            resp.raise_for_status()

    async def move_message(self, message_id: str, label: str) -> None:
        """Move message by adding/removing labels."""
        token = await self._acquire_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}"
                f"/messages/{message_id}/modify",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"addLabelIds": [label], "removeLabelIds": ["INBOX"]},
            )
            resp.raise_for_status()

    async def mark_read(self, message_id: str) -> None:
        """Mark message as read."""
        token = await self._acquire_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}"
                f"/messages/{message_id}/modify",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"removeLabelIds": ["UNREAD"]},
            )
            resp.raise_for_status()
