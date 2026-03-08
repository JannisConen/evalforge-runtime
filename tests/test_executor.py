"""Tests for the LLM executor (mocked LiteLLM)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evalforge_runtime.executor import Executor


@pytest.fixture
def executor() -> Executor:
    return Executor(default_model="anthropic/claude-haiku-4-5")


def _mock_response(content: str = '{"result": "ok"}', model: str = "claude-haiku-4-5"):
    """Create a mock LiteLLM response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.model = model
    response.usage = MagicMock()
    response.usage.prompt_tokens = 100
    response.usage.completion_tokens = 50
    return response


@pytest.mark.asyncio
async def test_execute_success(executor: Executor) -> None:
    """Successful LLM execution returns parsed output with tracking."""
    mock_resp = _mock_response('{"category": "invoice", "priority": 7}')

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = mock_resp

        result = await executor.execute(
            instructions="Categorize this email",
            input_data={"subject": "Invoice", "body": "Please pay"},
            process_name="email-categorizer",
        )

    assert result.output == {"category": "invoice", "priority": 7}
    assert result.llm_model == "claude-haiku-4-5"
    assert result.llm_tokens_in == 100
    assert result.llm_tokens_out == 50
    assert result.llm_cost_usd == 0.001
    assert result.llm_latency_ms is not None
    assert result.llm_latency_ms >= 0
    assert result.instructions_version is not None
    assert len(result.instructions_version) == 16  # SHA-256 truncated to 16


@pytest.mark.asyncio
async def test_execute_model_override(executor: Executor) -> None:
    """Model override is passed to LiteLLM."""
    mock_resp = _mock_response()

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.01),
    ):
        mock_llm.return_value = mock_resp

        await executor.execute(
            instructions="test",
            input_data={},
            process_name="test",
            model_override="openai/gpt-4o",
        )

        call_args = mock_llm.call_args
        assert call_args.kwargs["model"] == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_execute_cost_error_handled(executor: Executor) -> None:
    """Cost calculation failure doesn't break execution."""
    mock_resp = _mock_response()

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch(
            "evalforge_runtime.executor.litellm.completion_cost",
            side_effect=Exception("no pricing"),
        ),
    ):
        mock_llm.return_value = mock_resp

        result = await executor.execute(
            instructions="test",
            input_data={},
            process_name="test",
        )

    assert result.output == {"result": "ok"}
    assert result.llm_cost_usd is None


@pytest.mark.asyncio
async def test_execute_instructions_hash_deterministic(executor: Executor) -> None:
    """Same instructions produce the same hash."""
    mock_resp = _mock_response()

    with (
        patch("evalforge_runtime.executor.litellm.acompletion", new_callable=AsyncMock) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0),
    ):
        mock_llm.return_value = mock_resp

        r1 = await executor.execute(
            instructions="Categorize emails",
            input_data={},
            process_name="test",
        )
        r2 = await executor.execute(
            instructions="Categorize emails",
            input_data={},
            process_name="test",
        )

    assert r1.instructions_version == r2.instructions_version


@pytest.mark.asyncio
async def test_execute_llm_error_propagates(executor: Executor) -> None:
    """LLM errors propagate to caller."""
    with patch(
        "evalforge_runtime.executor.litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=Exception("API rate limited"),
    ):
        with pytest.raises(Exception, match="API rate limited"):
            await executor.execute(
                instructions="test",
                input_data={},
                process_name="test",
            )
