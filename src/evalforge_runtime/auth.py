"""API key authentication middleware."""

import os

from fastapi import HTTPException, Request

from evalforge_runtime.config import AuthConfig


class APIKeyAuth:
    """FastAPI dependency that validates API key authentication."""

    def __init__(self, config: AuthConfig):
        self.config = config
        self.api_key_methods = [m for m in config.methods if m.type == "api_key"]

    async def __call__(self, request: Request) -> str:
        """Validate the request. Returns the authenticated key.

        If EVALFORGE_API_KEYS is not set or empty, auth is skipped
        (allows local development without configuring keys).
        """
        valid_keys = self._get_valid_keys()
        if not valid_keys:
            return ""  # No keys configured — skip auth (local dev mode)

        for method in self.api_key_methods:
            header_name = method.header or "X-API-Key"
            key = request.headers.get(header_name)
            if key and key in valid_keys:
                return key

        raise HTTPException(status_code=401, detail="Unauthorized")

    def _get_valid_keys(self) -> list[str]:
        """Get list of valid API keys from environment.

        Includes EVALFORGE_API_KEYS (comma-separated) and EVALFORGE_SERVICE_KEY
        (project-scoped key for process-to-process calls).
        """
        keys: list[str] = []
        raw = os.environ.get("EVALFORGE_API_KEYS", "")
        keys.extend(k.strip() for k in raw.split(",") if k.strip())
        service_key = os.environ.get("EVALFORGE_SERVICE_KEY", "").strip()
        if service_key and service_key not in keys:
            keys.append(service_key)
        return keys
