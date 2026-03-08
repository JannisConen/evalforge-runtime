"""AWS Secrets Manager provider.

Requires optional extras: pip install evalforge-runtime[aws]
"""

from __future__ import annotations

import logging

from evalforge_runtime.secrets import SecretProvider

logger = logging.getLogger(__name__)


class AWSSecretsManagerProvider(SecretProvider):
    """Fetch secrets from AWS Secrets Manager.

    Expects a single secret containing a JSON dict of key-value pairs.
    AWS credentials come from the environment (IAM role, env vars, ~/.aws/credentials).
    """

    def __init__(self, region: str, secret_name: str):
        if not region:
            raise ValueError("aws_secrets_manager provider requires 'region' in secrets config")
        if not secret_name:
            raise ValueError(
                "aws_secrets_manager provider requires 'secret_name' in secrets config"
            )
        self.region = region
        self.secret_name = secret_name

    async def fetch(self) -> dict[str, str]:
        try:
            import boto3
        except ImportError:
            raise ImportError(
                "boto3 not installed. Install with: pip install evalforge-runtime[aws]"
            )

        import json

        client = boto3.client("secretsmanager", region_name=self.region)
        response = client.get_secret_value(SecretId=self.secret_name)

        secret_string = response.get("SecretString")
        if not secret_string:
            logger.warning(f"Secret '{self.secret_name}' has no string value")
            return {}

        data = json.loads(secret_string)
        if not isinstance(data, dict):
            raise ValueError(
                f"Secret '{self.secret_name}' must contain a JSON object, "
                f"got {type(data).__name__}"
            )

        return {str(k): str(v) for k, v in data.items()}
