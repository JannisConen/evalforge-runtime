"""Secret provider interface and manager."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

from evalforge_runtime.config import SecretConfig

logger = logging.getLogger(__name__)


class SecretProvider(ABC):
    """Base class for secret providers."""

    @abstractmethod
    async def fetch(self) -> dict[str, str]:
        """Fetch all secrets. Returns a dict of key→value."""
        ...


class EnvSecretProvider(SecretProvider):
    """Read secrets from environment variables."""

    async def fetch(self) -> dict[str, str]:
        return dict(os.environ)


class EvalForgeSecretProvider(SecretProvider):
    """Fetch secrets from EvalForge server."""

    def __init__(self, project_id: str, evalforge_url: str, env: str = "production"):
        self.project_id = project_id
        self.evalforge_url = evalforge_url.rstrip("/")
        self.env = env

    async def fetch(self) -> dict[str, str]:
        import httpx

        url = f"{self.evalforge_url}/api/v1/projects/{self.project_id}/secrets"
        params = {"env": self.env} if self.env else {}

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()


class SecretManager:
    """Loads secrets from the configured provider and injects into environment."""

    def __init__(self, config: SecretConfig, project_id: str = "", evalforge_url: str = ""):
        self.provider = self._init_provider(config, project_id, evalforge_url)

    async def load(self) -> dict[str, str]:
        """Load all secrets and inject into os.environ."""
        secrets = await self.provider.fetch()

        # Only inject secrets not already in environment (env vars take precedence)
        for key, value in secrets.items():
            if key not in os.environ:
                os.environ[key] = value

        return secrets

    def _init_provider(
        self, config: SecretConfig, project_id: str, evalforge_url: str
    ) -> SecretProvider:
        match config.provider:
            case "env":
                return EnvSecretProvider()
            case "evalforge":
                return EvalForgeSecretProvider(project_id, evalforge_url)
            case "azure_keyvault":
                from evalforge_runtime.secret_providers.azure_keyvault import (
                    AzureKeyVaultProvider,
                )
                return AzureKeyVaultProvider(vault_url=config.vault_url or "")
            case "aws_secrets_manager":
                from evalforge_runtime.secret_providers.aws_secrets import (
                    AWSSecretsManagerProvider,
                )
                return AWSSecretsManagerProvider(
                    region=config.region or "",
                    secret_name=config.secret_name or "",
                )
            case "sap_credential_store":
                from evalforge_runtime.secret_providers.sap_credential import (
                    SAPCredentialStoreProvider,
                )
                return SAPCredentialStoreProvider(instance=config.instance or "")
            case _:
                logger.warning(
                    f"Secret provider '{config.provider}' not yet implemented, "
                    "falling back to env"
                )
                return EnvSecretProvider()
