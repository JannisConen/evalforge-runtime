"""Send a new email."""

from __future__ import annotations

from typing import Any

from evalforge_runtime.actions.base import BaseAction, get_email_connector


class EmailSendAction(BaseAction):
    type = "email.send"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], context: dict[str, Any] | None = None) -> None:
        to = self.render_template(self.config.get("to", ""), output)
        subject = self.render_template(self.config.get("subject", ""), output)
        body = self.render_template(self.config.get("body", ""), output)

        connector = await get_email_connector(secrets, context)
        await connector.send_message(to=[to], subject=subject, body=body)
