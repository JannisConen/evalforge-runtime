"""Tests for the pipeline module — three-step execution and process chaining."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evalforge_runtime.config import AppConfig, ProcessConfig, TriggerConfig
from evalforge_runtime.db import close_db, get_execution, get_session_factory, init_db
from evalforge_runtime.executor import Executor
from evalforge_runtime.pipeline import Pipeline, _load_process_modules, _parse_duration
from evalforge_runtime.storage import LocalStorage
from evalforge_runtime.types import (
    After,
    Before,
    Execution,
    ExecutionContext,
    TriggerContext,
)


@pytest.fixture
def pipeline_config(tmp_path) -> AppConfig:
    """Config with multiple processes for chaining tests."""
    return AppConfig(
        project={"id": "test", "version": "0.1.0"},
        llm={"model": "test-model"},
        database={"url": "sqlite+aiosqlite://"},
        storage={"path": str(tmp_path / "files")},
        processes={
            "upstream": ProcessConfig(
                process_id="p1",
                instructions="Test instructions",
                trigger=TriggerConfig(type="webhook"),
            ),
            "downstream": ProcessConfig(
                process_id="p2",
                instructions="Downstream instructions",
                trigger=TriggerConfig(type="process", after="upstream"),
            ),
            "review-process": ProcessConfig(
                process_id="p3",
                instructions="Review instructions",
                trigger=TriggerConfig(type="webhook"),
                review={"enabled": True, "timeout": "1h"},
            ),
        },
    )


@pytest.fixture
async def db():
    await init_db("sqlite+aiosqlite://")
    yield
    await close_db()


@pytest.fixture
def executor():
    return Executor("test-model")


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(str(tmp_path / "files"))


def _mock_llm_response(content: str = '{"result": "ok"}'):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.model = "test-model"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    return response


# --- Duration parsing ---


def test_parse_duration_hours():
    assert _parse_duration("24h") == 86400


def test_parse_duration_minutes():
    assert _parse_duration("30m") == 1800


def test_parse_duration_days():
    assert _parse_duration("7d") == 604800


def test_parse_duration_seconds():
    assert _parse_duration("120s") == 120


# --- Pipeline execution ---


@pytest.mark.asyncio
async def test_pipeline_execute_default_llm(db, pipeline_config, executor, storage):
    """Pipeline executes with default LLM when no execution module."""
    pipeline = Pipeline(pipeline_config, executor, storage, secrets={})

    trigger = TriggerContext(type="webhook", ref="test-ref")

    with (
        patch(
            "evalforge_runtime.executor.litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = _mock_llm_response('{"category": "test"}')

        output = await pipeline.execute_process("upstream", {"data": "input"}, trigger)

    assert output == {"category": "test"}

    # Verify execution was logged
    session_factory = get_session_factory()
    async with session_factory() as session:
        from evalforge_runtime.db import list_executions

        execs = await list_executions(session, process_name="upstream")
        assert len(execs) == 1
        assert execs[0].status == "success"


@pytest.mark.asyncio
async def test_pipeline_review_flow(db, pipeline_config, executor, storage):
    """Pipeline pauses at pending_review when review is enabled."""
    pipeline = Pipeline(pipeline_config, executor, storage, secrets={})

    trigger = TriggerContext(type="webhook")

    with (
        patch(
            "evalforge_runtime.executor.litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = _mock_llm_response('{"draft": "response"}')

        output = await pipeline.execute_process(
            "review-process", {"email": "test"}, trigger
        )

    assert output == {"draft": "response"}

    # Verify execution is pending_review
    session_factory = get_session_factory()
    async with session_factory() as session:
        from evalforge_runtime.db import list_executions

        execs = await list_executions(session, process_name="review-process")
        assert len(execs) == 1
        assert execs[0].status == "pending_review"


@pytest.mark.asyncio
async def test_pipeline_approve_review(db, pipeline_config, executor, storage):
    """Approving a review transitions to success."""
    pipeline = Pipeline(pipeline_config, executor, storage, secrets={})

    trigger = TriggerContext(type="webhook")

    with (
        patch(
            "evalforge_runtime.executor.litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = _mock_llm_response('{"draft": "response"}')
        await pipeline.execute_process("review-process", {"email": "test"}, trigger)

    # Get the execution ID
    session_factory = get_session_factory()
    async with session_factory() as session:
        from evalforge_runtime.db import list_executions

        execs = await list_executions(session, process_name="review-process")
        exec_id = execs[0].id

    # Approve
    result = await pipeline.approve_review(exec_id, reviewed_by="tester")
    assert result == {"draft": "response"}

    # Verify status changed
    async with session_factory() as session:
        execution = await get_execution(session, exec_id)
        assert execution.status == "success"
        assert execution.reviewed_by == "tester"
        assert execution.review_modified is False


@pytest.mark.asyncio
async def test_pipeline_approve_with_modified_output(db, pipeline_config, executor, storage):
    """Approving with modified output updates the execution."""
    pipeline = Pipeline(pipeline_config, executor, storage, secrets={})

    trigger = TriggerContext(type="webhook")

    with (
        patch(
            "evalforge_runtime.executor.litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = _mock_llm_response('{"draft": "original"}')
        await pipeline.execute_process("review-process", {"email": "test"}, trigger)

    session_factory = get_session_factory()
    async with session_factory() as session:
        from evalforge_runtime.db import list_executions

        execs = await list_executions(session, process_name="review-process")
        exec_id = execs[0].id

    modified = {"draft": "edited by human"}
    result = await pipeline.approve_review(exec_id, modified_output=modified)
    assert result == modified

    async with session_factory() as session:
        execution = await get_execution(session, exec_id)
        assert execution.review_modified is True


@pytest.mark.asyncio
async def test_pipeline_reject_review(db, pipeline_config, executor, storage):
    """Rejecting a review sets status to rejected."""
    pipeline = Pipeline(pipeline_config, executor, storage, secrets={})

    trigger = TriggerContext(type="webhook")

    with (
        patch(
            "evalforge_runtime.executor.litellm.acompletion",
            new_callable=AsyncMock,
        ) as mock_llm,
        patch("evalforge_runtime.executor.litellm.completion_cost", return_value=0.001),
    ):
        mock_llm.return_value = _mock_llm_response('{"draft": "bad response"}')
        await pipeline.execute_process("review-process", {"email": "test"}, trigger)

    session_factory = get_session_factory()
    async with session_factory() as session:
        from evalforge_runtime.db import list_executions

        execs = await list_executions(session, process_name="review-process")
        exec_id = execs[0].id

    await pipeline.reject_review(exec_id, reason="Output was incorrect")

    async with session_factory() as session:
        execution = await get_execution(session, exec_id)
        assert execution.status == "rejected"
        assert execution.error == "Output was incorrect"


@pytest.mark.asyncio
async def test_pipeline_downstream_map(pipeline_config, executor, storage):
    """Pipeline correctly builds downstream process map."""
    pipeline = Pipeline(pipeline_config, executor, storage, secrets={})
    assert "upstream" in pipeline._downstream_map
    assert "downstream" in pipeline._downstream_map["upstream"]


# --- Types base classes ---


def test_before_condition_default():
    """Before.condition() defaults to True."""

    class TestBefore(Before):
        def prepare(self, source):
            return source

    b = TestBefore()
    assert b.condition({}) is True


def test_execution_config_default():
    """Execution accepts optional config."""

    class TestExec(Execution):
        async def run(self, input_data, context):
            return input_data

    e = TestExec()
    assert e.config == {}

    e2 = TestExec(config={"key": "val"})
    assert e2.config == {"key": "val"}


def test_after_config_default():
    """After accepts optional config."""

    class TestAfter(After):
        async def execute(self, trigger, output):
            pass

    a = TestAfter()
    assert a.config == {}
