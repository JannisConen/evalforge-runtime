"""API key authentication middleware."""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from evalforge_runtime.config import AuthConfig


class APIKeyAuth:
    """FastAPI dependency that validates API key authentication."""

    def __init__(self, config: AuthConfig):
        self.config = config
        self.api_key_methods = [m for m in config.methods if m.type == "api_key"]

    async def __call__(self, request: Request) -> str:
        """Validate the request. Returns the authenticated key."""
        for method in self.api_key_methods:
            header_name = method.header or "X-API-Key"
            key = request.headers.get(header_name)
            if key and self._validate_key(key):
                return key

        raise HTTPException(status_code=401, detail="Unauthorized")

    def _validate_key(self, key: str) -> bool:
        """Check if the key is in the allowed list."""
        valid_keys = os.environ.get("EVALFORGE_API_KEYS", "").split(",")
        valid_keys = [k.strip() for k in valid_keys if k.strip()]
        return key in valid_keys
