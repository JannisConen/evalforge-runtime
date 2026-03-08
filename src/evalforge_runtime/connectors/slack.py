"""Slack connector for sending messages.

Required secrets:
  SLACK_BOT_TOKEN
"""

from __future__ import annotations

from typing import Any

import httpx

from evalforge_runtime.connectors.base import Connector, ConnectorItem


class SlackConnector(Connector):
    """Slack connector — output only (send messages)."""

    def name(self) -> str:
        return "slack"

    async def validate(self) -> None:
        if "SLACK_BOT_TOKEN" not in self.secrets:
            raise ValueError("Slack connector requires SLACK_BOT_TOKEN secret")

    async def fetch(self) -> list[ConnectorItem]:
        """Slack connector is output-only."""
        return []

    async def send_message(self, channel: str, text: str) -> None:
        """Post a message to a Slack channel."""
        token = self.secrets["SLACK_BOT_TOKEN"]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"channel": channel, "text": text},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
