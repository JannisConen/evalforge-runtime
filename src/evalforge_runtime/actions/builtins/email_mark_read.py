"""Mark the triggering email as read."""

from __future__ import annotations

from typing import Any

from evalforge_runtime.actions.base import BaseAction, get_email_connector


class EmailMarkReadAction(BaseAction):
    type = "email.mark_read"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], context: dict[str, Any] | None = None) -> None:
        if not trigger.ref:
            raise ValueError("No trigger ref (message ID) — cannot mark read")

        connector = await get_email_connector(secrets, context)
        await connector.mark_read(trigger.ref)
