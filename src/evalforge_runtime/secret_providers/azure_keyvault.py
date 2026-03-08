"""Azure Key Vault secret provider.

Requires optional extras: pip install evalforge-runtime[azure]
"""

from __future__ import annotations

import logging

from evalforge_runtime.secrets import SecretProvider

logger = logging.getLogger(__name__)


class AzureKeyVaultProvider(SecretProvider):
    """Fetch secrets from Azure Key Vault using DefaultAzureCredential."""

    def __init__(self, vault_url: str):
        if not vault_url:
            raise ValueError("azure_keyvault provider requires 'vault_url' in secrets config")
        self.vault_url = vault_url

    async def fetch(self) -> dict[str, str]:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError:
            raise ImportError(
                "Azure Key Vault SDK not installed. "
                "Install with: pip install evalforge-runtime[azure]"
            )

        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=self.vault_url, credential=credential)

        secrets: dict[str, str] = {}
        for prop in client.list_properties_of_secrets():
            if not prop.enabled:
                continue
            secret = client.get_secret(prop.name)
            if secret.value is not None:
                # Azure KV uses hyphens in names; convert to env-var style
                key = prop.name.upper().replace("-", "_")
                secrets[key] = secret.value

        credential.close()
        return secrets
