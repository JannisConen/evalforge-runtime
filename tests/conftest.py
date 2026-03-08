"""Shared test fixtures."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from evalforge_runtime.config import AppConfig, load_config
from evalforge_runtime.db import Base, init_db, close_db

# Set API keys for tests
os.environ["EVALFORGE_API_KEYS"] = "test-key-123,another-key"


@pytest.fixture
def config_yaml(tmp_path: Path) -> Path:
    """Create a temporary config YAML file."""
    config_content = """
project:
  id: "test_proj"
  version: "0.1.0"

secrets:
  provider: env

auth:
  methods:
    - type: api_key
      header: "X-API-Key"

llm:
  model: "anthropic/claude-haiku-4-5"

database:
  url: "sqlite+aiosqlite://"

storage:
  type: local
  path: "{storage_path}"

processes:
  test-process:
    process_id: "proc_test"
    instructions: "You are a test processor. Return JSON: {{\\"result\\": \\"ok\\"}}"
    trigger:
      type: webhook
""".replace("{storage_path}", str(tmp_path / "files").replace("\\", "/"))
    config_file = tmp_path / "evalforge.config.yaml"
    config_file.write_text(config_content)
    return config_file


@pytest.fixture
def app_config(config_yaml: Path) -> AppConfig:
    """Load a test AppConfig."""
    return load_config(config_yaml)


@pytest_asyncio.fixture
async def db_session():
    """Initialize an in-memory database for testing."""
    await init_db("sqlite+aiosqlite://")
    yield
    await close_db()
