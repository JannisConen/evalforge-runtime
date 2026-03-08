"""Webhook connector — inbound HTTP trigger.

This connector doesn't fetch data; instead, it receives data via POST requests.
The server.py process endpoint acts as the webhook receiver.
"""

from __future__ import annotations

from typing import Any

from evalforge_runtime.connectors.base import Connector, ConnectorItem


class WebhookConnector(Connector):
    """Webhook connector — receives data via HTTP POST.

    This connector is passive; the process endpoint in server.py
    handles incoming requests. This class exists for consistency
    with the connector interface.
    """

    def name(self) -> str:
        return "webhook"

    async def fetch(self) -> list[ConnectorItem]:
        """Webhook connectors don't fetch — they receive via HTTP."""
        return []

    async def validate(self) -> None:
        """No validation needed for webhook connectors."""
        pass
