"""Tests for the database layer."""

from __future__ import annotations

import pytest

from evalforge_runtime.db import (
    create_execution,
    get_execution,
    get_session_factory,
    list_executions,
    update_execution,
)
from evalforge_runtime.observability import get_execution_stats


@pytest.mark.asyncio
async def test_create_and_get_execution(db_session: None) -> None:
    """Create an execution and retrieve it."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        record = await create_execution(
            session,
            execution_id="exec-001",
            process_name="test-process",
            process_id="proc_001",
            trigger_type="webhook",
            input_data={"subject": "Hello"},
            runtime_version="0.1.0",
            config_version="1.0.0",
        )
        assert record.id == "exec-001"
        assert record.status == "running"

    async with session_factory() as session:
        fetched = await get_execution(session, "exec-001")
        assert fetched is not None
        data = fetched.to_dict()
        assert data["process_name"] == "test-process"
        assert data["input"] == {"subject": "Hello"}
        assert data["status"] == "running"


@pytest.mark.asyncio
async def test_update_execution(db_session: None) -> None:
    """Update execution fields."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        await create_execution(
            session,
            execution_id="exec-002",
            process_name="test-process",
            process_id="proc_001",
            trigger_type="webhook",
            input_data={"x": 1},
        )

    async with session_factory() as session:
        await update_execution(
            session,
            "exec-002",
            output={"result": "ok"},
            status="success",
            llm_cost_usd=0.005,
            llm_tokens_in=100,
            llm_tokens_out=50,
        )

    async with session_factory() as session:
        fetched = await get_execution(session, "exec-002")
        assert fetched is not None
        data = fetched.to_dict()
        assert data["status"] == "success"
        assert data["output"] == {"result": "ok"}
        assert data["llm_cost_usd"] == 0.005


@pytest.mark.asyncio
async def test_list_executions_with_filters(db_session: None) -> None:
    """List executions with status filter."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        await create_execution(
            session,
            execution_id="exec-a",
            process_name="proc-a",
            process_id="p1",
            trigger_type="webhook",
            input_data={},
        )
    async with session_factory() as session:
        await update_execution(session, "exec-a", status="success")

    async with session_factory() as session:
        await create_execution(
            session,
            execution_id="exec-b",
            process_name="proc-b",
            process_id="p2",
            trigger_type="webhook",
            input_data={},
        )
    async with session_factory() as session:
        await update_execution(session, "exec-b", status="error")

    # Filter by status
    async with session_factory() as session:
        results = await list_executions(session, status=["success"])
        assert len(results) == 1
        assert results[0].id == "exec-a"

    # Filter by process name
    async with session_factory() as session:
        results = await list_executions(session, process_name="proc-b")
        assert len(results) == 1
        assert results[0].id == "exec-b"


@pytest.mark.asyncio
async def test_execution_stats(db_session: None) -> None:
    """Compute execution stats."""
    session_factory = get_session_factory()

    for i in range(5):
        async with session_factory() as session:
            await create_execution(
                session,
                execution_id=f"stat-{i}",
                process_name="test-process",
                process_id="p1",
                trigger_type="webhook",
                input_data={},
            )
        async with session_factory() as session:
            await update_execution(
                session,
                f"stat-{i}",
                status="success",
                llm_cost_usd=0.01,
                llm_tokens_in=100,
                llm_tokens_out=50,
                llm_latency_ms=500 + i * 100,
                llm_model="claude-haiku-4-5",
            )

    async with session_factory() as session:
        stats = await get_execution_stats(session)

    assert stats["total_executions"] == 5
    assert stats["success_rate"] == 1.0
    assert stats["total_cost_usd"] == pytest.approx(0.05)
    assert stats["avg_cost_usd"] == pytest.approx(0.01)
    assert stats["total_tokens_in"] == 500
    assert stats["total_tokens_out"] == 250
    assert stats["p95_latency_ms"] is not None
    assert stats["by_model"]["claude-haiku-4-5"]["total_executions"] == 5
