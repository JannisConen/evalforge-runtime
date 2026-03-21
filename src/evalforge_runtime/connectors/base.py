"""Connector base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ConnectorItem:
    """A single item fetched by a connector (e.g., one email).

    Files go as FileRef dicts in data["file"] so they match the API webhook format.
    """

    ref: str  # unique identifier (e.g., email message ID)
    data: dict[str, Any]


class Connector(ABC):
    """Base class for all connectors."""

    def __init__(self, params: dict[str, Any] | None = None, secrets: dict[str, str] | None = None):
        self.params = params or {}
        self.secrets = secrets or {}

    @abstractmethod
    async def fetch(self) -> list[ConnectorItem]:
        """Fetch new items from the source. Called on each trigger."""
        ...

    async def validate(self) -> None:
        """Validate credentials and config on startup. Raises on failure."""
        pass

    @abstractmethod
    def name(self) -> str:
        """Return the connector name (e.g., 'exchange-inbox')."""
        ...
