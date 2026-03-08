"""Tests for Langfuse integration via LiteLLM callbacks."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import litellm
import pytest

from evalforge_runtime.config import LangfuseConfig, ObservabilityConfig
from evalforge_runtime.executor import Executor


@pytest.fixture(autouse=True)
def _reset_litellm_callbacks():
    """Reset LiteLLM callbacks before and after each test."""
    original_success = litellm.success_callback[:]
    original_failure = litellm.failure_callback[:]
    yield
    litellm.success_callback = original_success
    litellm.failure_callback = original_failure


def test_langfuse_callbacks_set_when_enabled():
    obs = ObservabilityConfig(langfuse=LangfuseConfig(enabled=True))
    Executor("gpt-4o", observability=obs)

    assert "langfuse" in litellm.success_callback
    assert "langfuse" in litellm.failure_callback


def test_langfuse_callbacks_not_set_when_disabled():
    obs = ObservabilityConfig(langfuse=LangfuseConfig(enabled=False))
    Executor("gpt-4o", observability=obs)

    assert "langfuse" not in litellm.success_callback
    assert "langfuse" not in litellm.failure_callback


def test_langfuse_callbacks_not_set_when_no_observability():
    Executor("gpt-4o")

    assert "langfuse" not in litellm.success_callback
    assert "langfuse" not in litellm.failure_callback


def test_langfuse_host_set_from_config():
    obs = ObservabilityConfig(
        langfuse=LangfuseConfig(enabled=True, host="https://langfuse.example.com")
    )

    with patch.dict(os.environ, {}, clear=False):
        # Remove LANGFUSE_HOST if present
        os.environ.pop("LANGFUSE_HOST", None)
        Executor("gpt-4o", observability=obs)
        assert os.environ.get("LANGFUSE_HOST") == "https://langfuse.example.com"


def test_langfuse_host_not_overwritten_if_already_set():
    obs = ObservabilityConfig(
        langfuse=LangfuseConfig(enabled=True, host="https://new-host.com")
    )

    with patch.dict(os.environ, {"LANGFUSE_HOST": "https://existing-host.com"}, clear=False):
        Executor("gpt-4o", observability=obs)
        assert os.environ["LANGFUSE_HOST"] == "https://existing-host.com"


def test_langfuse_no_duplicate_callbacks():
    """Setting up Langfuse twice should not duplicate callbacks."""
    obs = ObservabilityConfig(langfuse=LangfuseConfig(enabled=True))
    Executor("gpt-4o", observability=obs)
    Executor("gpt-4o", observability=obs)

    assert litellm.success_callback.count("langfuse") == 1
    assert litellm.failure_callback.count("langfuse") == 1


@pytest.mark.asyncio
async def test_metadata_passed_to_acompletion():
    executor = Executor("gpt-4o")

    mock_response = AsyncMock()
    mock_response.choices = [AsyncMock()]
    mock_response.choices[0].message.content = '{"result": "ok"}'
    mock_response.usage = AsyncMock()
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    mock_response.model = "gpt-4o"

    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = mock_response

        with patch("litellm.completion_cost", return_value=0.001):
            await executor.execute(
                instructions="Test instructions",
                input_data={"key": "value"},
                process_name="test-process",
            )

        call_kwargs = mock_acompletion.call_args[1]
        assert "metadata" in call_kwargs
        assert call_kwargs["metadata"]["process_name"] == "test-process"
        assert "instructions_version" in call_kwargs["metadata"]
