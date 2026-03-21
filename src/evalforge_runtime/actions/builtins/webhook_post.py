"""POST JSON to a webhook URL."""

from __future__ import annotations

import json
from typing import Any

import httpx

from evalforge_runtime.actions.base import BaseAction


class WebhookPostAction(BaseAction):
    type = "webhook.post"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], **kwargs: Any) -> None:
        url = self.render_template(self.config.get("url", ""), output)
        body_template = self.config.get("body", '{"result": {{output}}}')
        headers_raw = self.config.get("headers", "{}")

        # Render body template
        rendered_body = self.render_template(body_template, output)

        # Parse headers
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if headers_raw:
            rendered_headers = self.render_template(str(headers_raw), output)
            try:
                extra = json.loads(rendered_headers)
                if isinstance(extra, dict):
                    headers.update(extra)
            except json.JSONDecodeError:
                pass

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, content=rendered_body, headers=headers)
            response.raise_for_status()
