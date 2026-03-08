"""Tests for config loading and validation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evalforge_runtime.config import AppConfig, load_config


def test_load_valid_config(config_yaml: Path) -> None:
    """Load a valid config file."""
    config = load_config(config_yaml)
    assert config.project.id == "test_proj"
    assert config.project.version == "0.1.0"
    assert config.llm.model == "anthropic/claude-haiku-4-5"
    assert "test-process" in config.processes
    assert config.processes["test-process"].process_id == "proc_test"
    assert config.processes["test-process"].trigger.type == "webhook"


def test_config_defaults() -> None:
    """Verify default values."""
    config = AppConfig(
        project={"id": "test"},
        processes={},
    )
    assert config.secrets.provider == "env"
    assert config.auth.methods == []
    assert config.llm.model == "anthropic/claude-sonnet-4-6"
    assert config.storage.type == "local"
    assert config.storage.path == "./data/files"
    assert config.database.url == "sqlite+aiosqlite:///./data/app.db"
    assert config.observability.langfuse.enabled is False


def test_env_var_resolution(tmp_path: Path) -> None:
    """Test ${ENV_VAR} substitution in config values."""
    os.environ["TEST_DB_URL"] = "sqlite+aiosqlite:///test.db"
    try:
        config_content = """
project:
  id: "test"
database:
  url: "${TEST_DB_URL}"
processes: {}
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)

        config = load_config(config_file)
        assert config.database.url == "sqlite+aiosqlite:///test.db"
    finally:
        del os.environ["TEST_DB_URL"]


def test_missing_config_file() -> None:
    """Error on missing config file."""
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/config.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    """Error on non-dict YAML."""
    config_file = tmp_path / "bad.yaml"
    config_file.write_text("just a string")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config(config_file)


def test_process_config_fields(config_yaml: Path) -> None:
    """Verify process config fields are parsed correctly."""
    config = load_config(config_yaml)
    proc = config.processes["test-process"]
    assert proc.instructions.startswith("You are a test processor")
    assert proc.review.enabled is False
    assert proc.llm_model is None
    assert proc.connector is None
