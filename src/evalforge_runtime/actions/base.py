"""Base action class and template rendering."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any


class BaseAction(ABC):
    """Abstract base for all action types."""

    type: str = "base"

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}

    @abstractmethod
    async def run(
        self,
        *,
        trigger: Any,
        output: dict[str, Any],
        secrets: dict[str, str],
        context: dict[str, Any] | None = None,
    ) -> None:
        """Execute the action.

        Args:
            trigger: TriggerContext (type, ref, source_execution_id).
            output: Process output dict.
            secrets: Available secrets dict.
            context: Runtime context with namespaced sections
                     (e.g. context["initiator"], context["process"]).
        """
        ...

    def render_template(self, template: str, output: dict[str, Any]) -> str:
        """Render {{output.field}} Mustache-style templates.

        Supports:
          - {{output.field}} — replaced with str(output["field"])
          - {{output}} — replaced with the full output JSON
        """
        import json

        def replace_match(m: re.Match) -> str:
            expr = m.group(1).strip()
            if expr == "output":
                return json.dumps(output)
            if expr.startswith("output."):
                key = expr[7:]  # strip "output."
                # Support nested dot paths
                value: Any = output
                for part in key.split("."):
                    if isinstance(value, dict):
                        value = value.get(part, "")
                    else:
                        value = ""
                        break
                return str(value) if value is not None else ""
            return m.group(0)  # unknown expression — leave as-is

        return re.sub(r"\{\{(.+?)\}\}", replace_match, template)


async def get_email_connector(secrets: dict[str, str], context: dict[str, Any] | None = None) -> Any:
    """Resolve the email connector from available secrets.

    Checks for Exchange credentials first, then Gmail (OAuth2 or service account).
    Uses context["initiator"] for connector params (mailbox, authMethod, etc.).
    """
    params = (context or {}).get("initiator", {})

    if "EXCHANGE_CLIENT_ID" in secrets:
        from evalforge_runtime.connectors.exchange import ExchangeConnector
        c = ExchangeConnector(params=params, secrets=secrets)
        await c.validate()
        return c

    # Gmail: App Password or service account
    has_app_pw = "GMAIL_APP_PASSWORD" in secrets
    has_sa = "GOOGLE_SERVICE_ACCOUNT_JSON" in secrets or "GMAIL_SERVICE_ACCOUNT_KEY" in secrets
    if has_app_pw or has_sa:
        from evalforge_runtime.connectors.gmail import GmailConnector
        c = GmailConnector(params=params, secrets=secrets)
        await c.validate()
        return c

    raise ValueError("No email connector credentials found in secrets")
