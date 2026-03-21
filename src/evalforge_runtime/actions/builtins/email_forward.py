"""Forward the triggering email."""

from __future__ import annotations

from typing import Any

from evalforge_runtime.actions.base import BaseAction, get_email_connector


class EmailForwardAction(BaseAction):
    type = "email.forward"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], context: dict[str, Any] | None = None) -> None:
        to = self.render_template(self.config.get("to", ""), output)
        body = self.render_template(self.config.get("body", ""), output)
        if not trigger.ref:
            raise ValueError("No trigger ref (message ID) — cannot forward")

        connector = await get_email_connector(secrets, context)
        await connector.forward(trigger.ref, to=to, body=body)
