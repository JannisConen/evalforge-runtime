"""Move the triggering email to a folder."""

from __future__ import annotations

from typing import Any

from evalforge_runtime.actions.base import BaseAction, get_email_connector


class EmailMoveAction(BaseAction):
    type = "email.move"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], context: dict[str, Any] | None = None) -> None:
        folder = self.config.get("folder", "Processed")
        if not trigger.ref:
            raise ValueError("No trigger ref (message ID) — cannot move")

        connector = await get_email_connector(secrets, context)
        await connector.move_message(trigger.ref, folder)
