"""Call another process in the same project."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from evalforge_runtime.actions.base import BaseAction

logger = logging.getLogger(__name__)


class ProcessCallAction(BaseAction):
    type = "process.call"

    async def run(self, *, trigger: Any, output: dict, secrets: dict[str, str], **kwargs: Any) -> None:
        target_name = self.config.get("targetProcessName", "")
        field_mappings = self.config.get("fieldMappings", [])

        if not target_name:
            raise ValueError("Target process name is required")

        # Build input from field mappings
        input_data = self._build_input(field_mappings, output)

        # Resolve target URL — same runtime, different process endpoint
        # The runtime base URL is passed via secrets or config
        base_url = secrets.get("RUNTIME_BASE_URL", "http://127.0.0.1:8000")
        # Normalize the process name to a slug (same as runtime server.py)
        slug = self._to_slug(target_name)
        url = f"{base_url.rstrip('/')}/process/{slug}"

        # Get API key for self-authentication
        api_key = secrets.get("EVALFORGE_API_KEY", "") or secrets.get("RUNTIME_API_KEY", "")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key

        logger.info(f"Calling process '{target_name}' at {url}")

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(url, json=input_data, headers=headers)
            response.raise_for_status()

        logger.info(f"Process '{target_name}' responded with status {response.status_code}")

    def _build_input(
        self, field_mappings: list[dict[str, Any]], output: dict[str, Any]
    ) -> dict[str, Any]:
        """Build input dict from field mappings, applying transforms."""
        result: dict[str, Any] = {}

        for mapping in field_mappings:
            source_path: str = mapping.get("source", "")
            target_path: str = mapping.get("target", "")
            transform_code: str | None = mapping.get("transform")

            if not source_path or not target_path:
                continue

            # Resolve source value from output using dot path
            value = self._resolve_path(output, source_path)

            # Apply optional Python transform
            if transform_code:
                value = self._apply_transform(transform_code, value, output)

            # Set value at target path (supports nested paths)
            self._set_path(result, target_path, value)

        return result

    def _resolve_path(self, data: dict[str, Any], path: str) -> Any:
        """Resolve a dot-notation path from a dict."""
        value: Any = data
        for part in path.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    def _set_path(self, data: dict[str, Any], path: str, value: Any) -> None:
        """Set a value at a dot-notation path, creating intermediate dicts."""
        parts = path.split(".")
        current = data
        for part in parts[:-1]:
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    def _apply_transform(
        self, code: str, value: Any, output: dict[str, Any]
    ) -> Any:
        """Execute a Python transform function.

        The code must define: def transform(value, output) -> Any
        """
        try:
            local_ns: dict[str, Any] = {}
            exec(code, {"__builtins__": __builtins__}, local_ns)
            transform_fn = local_ns.get("transform")
            if callable(transform_fn):
                return transform_fn(value, output)
            logger.warning("Transform code does not define 'transform(value, output)'")
            return value
        except Exception as e:
            logger.error(f"Transform failed: {e}")
            return value

    def _to_slug(self, name: str) -> str:
        """Convert process name to URL slug (matches runtime server.py)."""
        import re
        # camelCase → kebab-case
        slug = re.sub(r"([a-z])([A-Z])", r"\1-\2", name)
        # spaces/underscores → dashes
        slug = re.sub(r"[\s_]+", "-", slug)
        return slug.lower()
