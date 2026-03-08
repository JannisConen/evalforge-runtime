"""Tests for connectors and supporting modules."""

from __future__ import annotations

import os

import pytest

from evalforge_runtime.connectors.base import Connector, ConnectorItem
from evalforge_runtime.connectors.webhook import WebhookConnector
from evalforge_runtime.connectors.slack import SlackConnector
from evalforge_runtime.config import _parse_duration
from evalforge_runtime.scheduler import Scheduler
from evalforge_runtime.secrets import EnvSecretProvider, SecretManager, SecretConfig
from evalforge_runtime.types import FileRef


# --- ConnectorItem ---


def test_connector_item_basic():
    item = ConnectorItem(ref="msg-123", data={"subject": "Test"})
    assert item.ref == "msg-123"
    assert item.data["subject"] == "Test"
    assert item.attachments == []


def test_connector_item_with_attachments():
    att = FileRef(
        type="local",
        key="files/test.pdf",
        filename="test.pdf",
        size=1024,
        mimeType="application/pdf",
        extension="pdf",
    )
    item = ConnectorItem(ref="msg-123", data={}, attachments=[att])
    assert len(item.attachments) == 1
    assert item.attachments[0].filename == "test.pdf"


# --- WebhookConnector ---


@pytest.mark.asyncio
async def test_webhook_connector_fetch_empty():
    conn = WebhookConnector()
    assert await conn.fetch() == []
    assert conn.name() == "webhook"


@pytest.mark.asyncio
async def test_webhook_connector_validate():
    conn = WebhookConnector()
    await conn.validate()  # should not raise


# --- SlackConnector ---


def test_slack_connector_name():
    conn = SlackConnector()
    assert conn.name() == "slack"


@pytest.mark.asyncio
async def test_slack_validate_missing_token():
    conn = SlackConnector(secrets={})
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        await conn.validate()


@pytest.mark.asyncio
async def test_slack_validate_with_token():
    conn = SlackConnector(secrets={"SLACK_BOT_TOKEN": "xoxb-test"})
    await conn.validate()  # should not raise


@pytest.mark.asyncio
async def test_slack_fetch_empty():
    conn = SlackConnector()
    assert await conn.fetch() == []


# --- SecretManager ---


@pytest.mark.asyncio
async def test_env_secret_provider():
    provider = EnvSecretProvider()
    secrets = await provider.fetch()
    # Should contain env vars
    assert isinstance(secrets, dict)
    assert "EVALFORGE_API_KEYS" in secrets  # set in conftest


@pytest.mark.asyncio
async def test_secret_manager_env():
    config = SecretConfig(provider="env")
    manager = SecretManager(config)
    secrets = await manager.load()
    assert isinstance(secrets, dict)


@pytest.mark.asyncio
async def test_secret_manager_injects_env():
    """SecretManager injects secrets that don't exist in env."""
    config = SecretConfig(provider="env")
    manager = SecretManager(config)

    # The env provider returns all existing env vars, so injection
    # only adds vars not already present (noop for env provider)
    secrets = await manager.load()
    assert len(secrets) > 0


# --- Scheduler ---


@pytest.mark.asyncio
async def test_scheduler_start_stop_without_apscheduler():
    """Scheduler degrades gracefully without APScheduler."""
    sched = Scheduler()
    # start() should log warning but not crash
    await sched.start()
    await sched.stop()


def test_scheduler_add_job_without_start():
    """Adding a job without starting scheduler logs warning."""
    sched = Scheduler()

    async def dummy():
        pass

    sched.add_cron_job("test", "*/5 * * * *", dummy)
    assert sched.job_ids == []


# --- Duration parsing ---


def test_parse_duration_variants():
    assert _parse_duration("24h") == 86400
    assert _parse_duration("30m") == 1800
    assert _parse_duration("7d") == 604800
    assert _parse_duration("60s") == 60
    assert _parse_duration("3600") == 3600
