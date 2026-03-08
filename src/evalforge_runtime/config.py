"""Pydantic models for evalforge.config.yaml and config loading."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel


class ProjectConfig(BaseModel):
    id: str
    evalforge_url: str | None = None
    version: str = "0.0.0"


class SecretConfig(BaseModel):
    provider: Literal[
        "evalforge", "env", "azure_keyvault", "aws_secrets_manager", "sap_credential_store"
    ] = "env"
    # Azure Key Vault
    vault_url: str | None = None
    # AWS Secrets Manager
    region: str | None = None
    secret_name: str | None = None
    # SAP Credential Store
    instance: str | None = None


class AuthMethod(BaseModel):
    type: Literal["oauth2", "api_key"]
    issuer: str | None = None
    audience: str | None = None
    header: str | None = None


class AuthConfig(BaseModel):
    methods: list[AuthMethod] = []


class LLMConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-4-6"


class ReviewConfig(BaseModel):
    enabled: bool = False
    timeout: str = "24h"


class LangfuseConfig(BaseModel):
    enabled: bool = False
    host: str | None = None


class ObservabilityConfig(BaseModel):
    langfuse: LangfuseConfig = LangfuseConfig()


class TriggerConfig(BaseModel):
    type: Literal["schedule", "webhook", "process"]
    cron: str | None = None
    after: str | None = None


class ProcessConfig(BaseModel):
    process_id: str
    instructions: str = ""
    trigger: TriggerConfig
    connector: str | None = None
    connector_params: dict[str, Any] | None = None
    review: ReviewConfig = ReviewConfig()
    llm_model: str | None = None
    output_schema: dict[str, str] | None = None
    before_module: str | None = None
    execution_module: str | None = None
    after_module: str | None = None


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///./data/app.db"


class StorageConfig(BaseModel):
    type: Literal["local", "s3"] = "local"
    path: str = "./data/files"
    bucket: str | None = None
    region: str | None = None


class AppConfig(BaseModel):
    project: ProjectConfig
    secrets: SecretConfig = SecretConfig()
    auth: AuthConfig = AuthConfig()
    llm: LLMConfig = LLMConfig()
    database: DatabaseConfig = DatabaseConfig()
    storage: StorageConfig = StorageConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    processes: dict[str, ProcessConfig] = {}


def _parse_duration(duration_str: str) -> int:
    """Parse duration string like '24h', '30m', '7d' to seconds."""
    duration_str = duration_str.strip().lower()
    if duration_str.endswith("d"):
        return int(duration_str[:-1]) * 86400
    elif duration_str.endswith("h"):
        return int(duration_str[:-1]) * 3600
    elif duration_str.endswith("m"):
        return int(duration_str[:-1]) * 60
    elif duration_str.endswith("s"):
        return int(duration_str[:-1])
    else:
        return int(duration_str)


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)}")


def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve ${ENV_VAR} patterns in config values."""
    if isinstance(value, str):
        def replacer(match: re.Match[str]) -> str:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                return match.group(0)  # leave unresolved
            return env_value

        return _ENV_VAR_PATTERN.sub(replacer, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def load_config(path: str | Path) -> AppConfig:
    """Load and validate an evalforge.config.yaml file."""
    from dotenv import load_dotenv

    load_dotenv()

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a YAML mapping, got {type(raw).__name__}")

    resolved = _resolve_env_vars(raw)
    return AppConfig.model_validate(resolved)
