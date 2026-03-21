"""Gmail / Google Workspace connector.

Two authentication methods:
  1. App Password (recommended for personal/small team) — uses IMAP/SMTP
  2. Service Account (enterprise) — uses Gmail API with domain-wide delegation

Required secrets (App Password):
  GMAIL_APP_PASSWORD — App password generated in Google Account settings

Required secrets (Service Account):
  GOOGLE_SERVICE_ACCOUNT_JSON — service account key (JSON string)
"""

from __future__ import annotations

import asyncio
import base64
import email as email_lib
import email.mime.text
import imaplib
import json
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from evalforge_runtime.connectors.base import Connector, ConnectorItem
from evalforge_runtime.storage import LocalStorage

logger = logging.getLogger(__name__)


class GmailConnector(Connector):
    """Gmail connector with App Password (IMAP/SMTP) or Service Account (API) auth."""

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
        self._auth_method = self.params.get("authMethod", self._detect_auth_method())
        self._max_age_minutes = int(
            self.params.get("maxAgeMinutes", os.environ.get("EMAIL_MAX_AGE_MINUTES", "1440"))
        )

    def _detect_auth_method(self) -> str:
        """Auto-detect auth method from available secrets."""
        if "GMAIL_APP_PASSWORD" in self.secrets:
            return "app_password"
        if "GOOGLE_SERVICE_ACCOUNT_JSON" in self.secrets:
            return "service_account"
        return "app_password"  # default

    def name(self) -> str:
        return "gmail-inbox"

    async def validate(self) -> None:
        """Validate Gmail credentials."""
        logger.info("Validating Gmail connector (auth_method=%s, mailbox=%s)", self._auth_method, self._mailbox)
        if self._auth_method == "service_account":
            if "GOOGLE_SERVICE_ACCOUNT_JSON" not in self.secrets:
                raise ValueError("Gmail service account requires GOOGLE_SERVICE_ACCOUNT_JSON secret")
            if not self._mailbox or self._mailbox == "me":
                raise ValueError("Gmail service account requires explicit 'mailbox' parameter for delegation")
        else:
            # app_password (or any legacy value like "oauth2")
            if "GMAIL_APP_PASSWORD" not in self.secrets:
                raise ValueError("Gmail App Password mode requires GMAIL_APP_PASSWORD secret")
            if not self._mailbox or self._mailbox == "me":
                raise ValueError("Gmail requires 'mailbox' parameter (your email address)")
        logger.info("Gmail connector validation passed")

    # -----------------------------------------------------------------------
    # Token acquisition (Service Account only — App Password uses IMAP/SMTP)
    # -----------------------------------------------------------------------

    async def _acquire_token(self) -> str:
        """Acquire access token for Gmail API (service account only)."""
        if self._token:
            return self._token

        logger.debug("Acquiring Gmail API token via service account")
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
            logger.info("Gmail API token acquired successfully")
        except ImportError:
            raise RuntimeError(
                "Gmail service account auth requires google-auth package. "
                "Install with: pip install google-auth"
            )

        return self._token

    # -----------------------------------------------------------------------
    # Fetch (IMAP for app_password, Gmail API for service_account)
    # -----------------------------------------------------------------------

    async def fetch(self) -> list[ConnectorItem]:
        """Fetch messages matching the filter."""
        logger.info("Fetching emails (auth_method=%s, filter=%s)", self._auth_method, self._filter)
        if self._auth_method != "service_account":
            return await self._fetch_imap()
        return await self._fetch_api()

    async def _fetch_imap(self) -> list[ConnectorItem]:
        """Fetch unread messages via IMAP (app password auth)."""
        def _do_fetch() -> list[tuple[str, bytes, dict[str, str]]]:
            """Returns list of (ref, raw_bytes, parsed_data) tuples."""
            logger.debug("Connecting to imap.gmail.com as %s", self._mailbox)
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(self._mailbox, self.secrets["GMAIL_APP_PASSWORD"])
            mail.select("INBOX")

            # Build IMAP search criteria with date filter
            since_date = (datetime.now(timezone.utc) - timedelta(minutes=self._max_age_minutes)).strftime("%d-%b-%Y")
            if self._filter in ("unread", "is:unread"):
                search_criteria = f'(UNSEEN SINCE {since_date})'
            else:
                search_criteria = f'(SINCE {since_date})'
            logger.debug("IMAP search criteria: %s", search_criteria)
            _, msg_ids = mail.search(None, search_criteria)
            ids = msg_ids[0].split()[-50:]  # limit to last 50

            results: list[tuple[str, bytes, dict[str, str]]] = []
            for msg_id in ids:
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0]
                if not isinstance(raw, tuple):
                    continue

                raw_bytes: bytes = raw[1]
                msg = email_lib.message_from_bytes(raw_bytes)
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode("utf-8", errors="replace")
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")

                # Detect attachments
                has_attachments = False
                if msg.is_multipart():
                    for part in msg.walk():
                        cd = str(part.get("Content-Disposition", ""))
                        if "attachment" in cd:
                            has_attachments = True
                            break

                ref = msg_id.decode("utf-8")
                results.append((ref, raw_bytes, {
                    "email_subject": str(msg.get("Subject", "")),
                    "email_body": body,
                    "sender": str(msg.get("From", "")),
                    "received_at": str(msg.get("Date", "")),
                    "has_attachments": has_attachments,
                }))

            mail.logout()
            logger.info("IMAP fetch complete: %d messages retrieved", len(results))
            return results

        raw_messages = await asyncio.to_thread(_do_fetch)

        # Save .eml files and build ConnectorItems (async context)
        items: list[ConnectorItem] = []
        for ref, raw_bytes, data in raw_messages:
            if self.storage:
                subject_slug = _slugify(data.get("email_subject", "email"))[:60]
                eml_filename = f"{subject_slug}.eml"
                eml_key = f"connector/gmail/{ref}/{eml_filename}"
                if not await self.storage.exists(eml_key):
                    await self.storage.put(eml_key, raw_bytes, "message/rfc822")
                    logger.info("Saved .eml file: %s (%d bytes)", eml_key, len(raw_bytes))
                # Put FileRef as data["file"] — same structure as API webhook input
                data["file"] = {
                    "type": "local",
                    "key": eml_key,
                    "filename": eml_filename,
                    "size": len(raw_bytes),
                    "mimeType": "message/rfc822",
                    "extension": "eml",
                }

            items.append(ConnectorItem(ref=ref, data=data))

        return items

    async def _fetch_api(self) -> list[ConnectorItem]:
        """Fetch messages via Gmail API (service account auth)."""
        logger.debug("Fetching messages via Gmail API for %s", self._mailbox)
        token = await self._acquire_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Add date filter to Gmail search query
        since_date = (datetime.now(timezone.utc) - timedelta(minutes=self._max_age_minutes)).strftime("%Y/%m/%d")
        query = f"{self._filter} after:{since_date}"
        logger.debug("Gmail API query: %s", query)

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}/messages",
                headers=headers,
                params={"q": query, "maxResults": 50},
            )
            resp.raise_for_status()
            message_ids = [m["id"] for m in resp.json().get("messages", [])]
            logger.debug("Gmail API listed %d messages", len(message_ids))

        items: list[ConnectorItem] = []
        async with httpx.AsyncClient() as client:
            for msg_id in message_ids:
                # Fetch full message for metadata
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

                # Detect attachments from parts
                has_attachments = self._has_attachments_api(msg.get("payload", {}))

                data = {
                    "email_subject": headers_map.get("subject", ""),
                    "email_body": self._extract_body_api(msg.get("payload", {})),
                    "sender": headers_map.get("from", ""),
                    "received_at": headers_map.get("date", ""),
                    "has_attachments": has_attachments,
                }

                # Fetch raw RFC822 and save as .eml (skip if already saved)
                if self.storage:
                    subject_slug = _slugify(data.get("email_subject", "email"))[:60]
                    eml_filename = f"{subject_slug}.eml"
                    eml_key = f"connector/gmail/{msg_id}/{eml_filename}"
                    if not await self.storage.exists(eml_key):
                        raw_resp = await client.get(
                            f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}"
                            f"/messages/{msg_id}?format=raw",
                            headers=headers,
                        )
                        raw_resp.raise_for_status()
                        raw_b64 = raw_resp.json().get("raw", "")
                        raw_bytes = base64.urlsafe_b64decode(raw_b64)
                        await self.storage.put(eml_key, raw_bytes, "message/rfc822")
                        eml_size = len(raw_bytes)
                        logger.info("Saved .eml file: %s (%d bytes)", eml_key, eml_size)
                    else:
                        eml_size = await self.storage.size(eml_key)
                    # Put FileRef as data["file"] — same structure as API webhook input
                    data["file"] = {
                        "type": "local",
                        "key": eml_key,
                        "filename": eml_filename,
                        "size": eml_size,
                        "mimeType": "message/rfc822",
                        "extension": "eml",
                    }

                items.append(ConnectorItem(ref=msg_id, data=data))

        logger.info("Gmail API fetch complete: %d messages retrieved", len(items))
        return items

    def _extract_body_api(self, payload: dict) -> str:
        """Extract text body from Gmail API message payload."""
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

        for part in payload.get("parts", []):
            body = self._extract_body_api(part)
            if body:
                return body

        return ""

    def _has_attachments_api(self, payload: dict) -> bool:
        """Check if any part in the Gmail API payload is an attachment."""
        filename = payload.get("filename")
        if filename:
            return True
        for part in payload.get("parts", []):
            if self._has_attachments_api(part):
                return True
        return False

    # -----------------------------------------------------------------------
    # Send (SMTP for app_password, Gmail API for service_account)
    # -----------------------------------------------------------------------

    async def send_message(
        self, to: list[str], subject: str, body: str,
    ) -> str:
        """Send a new email."""
        logger.info("Sending email to %s (subject=%s)", to, subject[:80])
        if self._auth_method != "service_account":
            return await self._send_smtp(to, subject, body)
        return await self._send_api(to, subject, body)

    async def _send_smtp(self, to: list[str], subject: str, body: str) -> str:
        """Send email via SMTP (app password auth)."""
        def _do_send() -> str:
            msg = email.mime.text.MIMEText(body, "html")
            msg["From"] = self._mailbox
            msg["To"] = ", ".join(to)
            msg["Subject"] = subject

            logger.debug("Connecting to smtp.gmail.com as %s", self._mailbox)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self._mailbox, self.secrets["GMAIL_APP_PASSWORD"])
                server.send_message(msg)
            logger.info("Email sent via SMTP to %s", msg["To"])
            return "sent"

        return await asyncio.to_thread(_do_send)

    async def _send_api(self, to: list[str], subject: str, body: str) -> str:
        """Send email via Gmail API (service account auth)."""
        token = await self._acquire_token()

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
            msg_id = resp.json().get("id", "sent")
            logger.info("Email sent via Gmail API (id=%s)", msg_id)
            return msg_id

    # -----------------------------------------------------------------------
    # Reply
    # -----------------------------------------------------------------------

    async def reply(self, message_id: str, body: str, reply_all: bool = False) -> None:
        """Reply to a message."""
        logger.info("Replying to message %s", message_id)
        if self._auth_method != "service_account":
            await self._reply_smtp(message_id, body)
        else:
            await self._reply_api(message_id, body)

    async def _reply_smtp(self, message_id: str, body: str) -> None:
        """Reply via SMTP. Fetches original via IMAP for headers."""
        def _do_reply() -> None:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(self._mailbox, self.secrets["GMAIL_APP_PASSWORD"])
            mail.select("INBOX")

            # Fetch original message for reply headers
            _, msg_data = mail.fetch(message_id.encode(), "(RFC822)")
            if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                raise ValueError(f"Could not fetch original message {message_id}")

            original = email_lib.message_from_bytes(msg_data[0][1])

            reply_msg = email.mime.text.MIMEText(body, "html")
            reply_msg["From"] = self._mailbox
            reply_msg["To"] = str(original.get("From", ""))
            reply_msg["Subject"] = "Re: " + str(original.get("Subject", ""))
            reply_msg["In-Reply-To"] = str(original.get("Message-ID", ""))
            reply_msg["References"] = str(original.get("Message-ID", ""))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self._mailbox, self.secrets["GMAIL_APP_PASSWORD"])
                server.send_message(reply_msg)
            logger.info("Reply sent via SMTP to %s", reply_msg["To"])

            mail.logout()

        await asyncio.to_thread(_do_reply)

    async def _reply_api(self, message_id: str, body: str) -> None:
        """Reply via Gmail API (service account auth)."""
        token = await self._acquire_token()

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
            logger.info("Reply sent via Gmail API (thread=%s)", thread_id)

    # -----------------------------------------------------------------------
    # Move / Mark read
    # -----------------------------------------------------------------------

    async def move_message(self, message_id: str, label: str) -> None:
        """Move message by adding/removing labels."""
        logger.info("Moving message %s to %s", message_id, label)
        if self._auth_method != "service_account":
            await self._move_imap(message_id, label)
        else:
            await self._move_api(message_id, label)

    async def _move_imap(self, message_id: str, label: str) -> None:
        """Move message via IMAP."""
        def _do_move() -> None:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(self._mailbox, self.secrets["GMAIL_APP_PASSWORD"])
            mail.select("INBOX")
            mail.copy(message_id.encode(), label)
            mail.store(message_id.encode(), "+FLAGS", "\\Deleted")
            mail.expunge()
            mail.logout()
            logger.info("Message %s moved to %s via IMAP", message_id, label)

        await asyncio.to_thread(_do_move)

    async def _move_api(self, message_id: str, label: str) -> None:
        """Move message via Gmail API."""
        token = await self._acquire_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}"
                f"/messages/{message_id}/modify",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"addLabelIds": [label], "removeLabelIds": ["INBOX"]},
            )
            resp.raise_for_status()
            logger.info("Message %s moved to %s via Gmail API", message_id, label)

    async def mark_read(self, message_id: str) -> None:
        """Mark message as read."""
        logger.info("Marking message %s as read", message_id)
        if self._auth_method != "service_account":
            await self._mark_read_imap(message_id)
        else:
            await self._mark_read_api(message_id)

    async def _mark_read_imap(self, message_id: str) -> None:
        """Mark message as read via IMAP."""
        def _do_mark() -> None:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(self._mailbox, self.secrets["GMAIL_APP_PASSWORD"])
            mail.select("INBOX")
            mail.store(message_id.encode(), "+FLAGS", "\\Seen")
            mail.logout()
            logger.debug("Message %s marked as read via IMAP", message_id)

        await asyncio.to_thread(_do_mark)

    async def _mark_read_api(self, message_id: str) -> None:
        """Mark message as read via Gmail API."""
        token = await self._acquire_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://gmail.googleapis.com/gmail/v1/users/{self._mailbox}"
                f"/messages/{message_id}/modify",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"removeLabelIds": ["UNREAD"]},
            )
            resp.raise_for_status()
            logger.debug("Message %s marked as read via Gmail API", message_id)


import re as _re

def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = _re.sub(r"[^\w\s-]", "", text)
    text = _re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "email"
