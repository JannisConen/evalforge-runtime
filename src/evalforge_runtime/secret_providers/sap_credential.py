"""SAP BTP Credential Store provider.

Reads credentials from SAP BTP Credential Store via XSUAA-authenticated REST API.
Binding info comes from VCAP_SERVICES environment variable.
"""

from __future__ import annotations

import json
import logging
import os

from evalforge_runtime.secrets import SecretProvider

logger = logging.getLogger(__name__)


class SAPCredentialStoreProvider(SecretProvider):
    """Fetch secrets from SAP BTP Credential Store."""

    def __init__(self, instance: str = ""):
        self.instance = instance

    async def fetch(self) -> dict[str, str]:
        import httpx

        binding = self._get_binding()
        token = await self._get_token(binding)

        credentials_url = binding["credentials"]["url"]
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{credentials_url}/api/v1/credentials",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

        secrets: dict[str, str] = {}
        for cred in data.get("credentials", []):
            name = cred.get("name", "")
            value = cred.get("value", "")
            if name and value:
                secrets[name] = value

        return secrets

    def _get_binding(self) -> dict:
        """Extract credential store binding from VCAP_SERVICES."""
        vcap_raw = os.environ.get("VCAP_SERVICES", "{}")
        try:
            vcap = json.loads(vcap_raw)
        except json.JSONDecodeError:
            raise ValueError("VCAP_SERVICES is not valid JSON")

        # Look for credstore service binding
        for service_name, instances in vcap.items():
            if "credstore" in service_name.lower() or "credential" in service_name.lower():
                for inst in instances:
                    if not self.instance or inst.get("instance_name") == self.instance:
                        return inst

        raise ValueError(
            f"No credential store binding found in VCAP_SERVICES"
            + (f" for instance '{self.instance}'" if self.instance else "")
        )

    async def _get_token(self, binding: dict) -> str:
        """Get OAuth token from XSUAA."""
        import httpx

        creds = binding.get("credentials", {})
        uaa = creds.get("uaa", {})
        token_url = uaa.get("url", "") + "/oauth/token"
        client_id = uaa.get("clientid", "")
        client_secret = uaa.get("clientsecret", "")

        if not all([token_url, client_id, client_secret]):
            raise ValueError("Incomplete XSUAA binding in VCAP_SERVICES")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                token_url,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["access_token"]
