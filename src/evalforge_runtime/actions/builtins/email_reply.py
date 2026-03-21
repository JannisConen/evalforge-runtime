"""Reply to the triggering email."""

from __future__ import annotations

from typing import Any

from evalforge_runtime.actions.base import BaseAction, get_email_connector


class EmailReplyAction(BaseAction):
    type = "email.reply"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], context: dict[str, Any] | None = None) -> None:
        body = self.render_template(self.config.get("body", ""), output)
        if not trigger.ref:
            raise ValueError("No trigger ref (message ID) — cannot reply")

        connector = await get_email_connector(secrets, context)
        await connector.reply(trigger.ref, body=body)
