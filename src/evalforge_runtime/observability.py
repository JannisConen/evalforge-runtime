"""Execution stats aggregation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge_runtime.db import ExecutionRecord


async def get_execution_stats(
    session: AsyncSession,
    *,
    process_name: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> dict[str, Any]:
    """Compute aggregated execution statistics."""
    query = select(
        func.count(ExecutionRecord.id).label("total"),
        func.sum(
            case((ExecutionRecord.status == "success", 1), else_=0)
        ).label("success_count"),
        func.sum(ExecutionRecord.llm_cost_usd).label("total_cost"),
        func.avg(ExecutionRecord.llm_cost_usd).label("avg_cost"),
        func.avg(ExecutionRecord.llm_latency_ms).label("avg_latency"),
        func.sum(ExecutionRecord.llm_tokens_in).label("total_tokens_in"),
        func.sum(ExecutionRecord.llm_tokens_out).label("total_tokens_out"),
    )

    if process_name:
        query = query.where(ExecutionRecord.process_name == process_name)
    if from_date:
        query = query.where(ExecutionRecord.started_at >= from_date)
    if to_date:
        query = query.where(ExecutionRecord.started_at <= to_date)

    result = await session.execute(query)
    row = result.one()

    total = row.total or 0
    success_count = row.success_count or 0

    # p95 latency — fetch all latencies and compute in Python (fine for Phase 1 volumes)
    p95_latency: float | None = None
    if total > 0:
        latency_query = (
            select(ExecutionRecord.llm_latency_ms)
            .where(ExecutionRecord.llm_latency_ms.is_not(None))
        )
        if process_name:
            latency_query = latency_query.where(
                ExecutionRecord.process_name == process_name
            )
        if from_date:
            latency_query = latency_query.where(ExecutionRecord.started_at >= from_date)
        if to_date:
            latency_query = latency_query.where(ExecutionRecord.started_at <= to_date)

        latency_result = await session.execute(latency_query)
        latencies = sorted([r for (r,) in latency_result.all() if r is not None])
        if latencies:
            idx = int(len(latencies) * 0.95)
            idx = min(idx, len(latencies) - 1)
            p95_latency = float(latencies[idx])

    # Breakdown by process
    by_process: dict[str, Any] = {}
    if not process_name:
        proc_query = select(
            ExecutionRecord.process_name,
            func.count(ExecutionRecord.id).label("count"),
            func.sum(ExecutionRecord.llm_cost_usd).label("cost"),
        ).group_by(ExecutionRecord.process_name)
        if from_date:
            proc_query = proc_query.where(ExecutionRecord.started_at >= from_date)
        if to_date:
            proc_query = proc_query.where(ExecutionRecord.started_at <= to_date)

        proc_result = await session.execute(proc_query)
        for pname, count, cost in proc_result.all():
            by_process[pname] = {"total_executions": count, "total_cost_usd": cost}

    # Breakdown by model
    model_query = select(
        ExecutionRecord.llm_model,
        func.count(ExecutionRecord.id).label("count"),
        func.sum(ExecutionRecord.llm_cost_usd).label("cost"),
    ).where(ExecutionRecord.llm_model.is_not(None)).group_by(ExecutionRecord.llm_model)
    if process_name:
        model_query = model_query.where(ExecutionRecord.process_name == process_name)
    if from_date:
        model_query = model_query.where(ExecutionRecord.started_at >= from_date)
    if to_date:
        model_query = model_query.where(ExecutionRecord.started_at <= to_date)

    model_result = await session.execute(model_query)
    by_model: dict[str, Any] = {}
    for model_name, count, cost in model_result.all():
        if model_name:
            by_model[model_name] = {"total_executions": count, "total_cost_usd": cost}

    return {
        "total_executions": total,
        "success_rate": success_count / total if total > 0 else 0,
        "total_cost_usd": float(row.total_cost) if row.total_cost else 0,
        "avg_cost_usd": float(row.avg_cost) if row.avg_cost else 0,
        "avg_latency_ms": float(row.avg_latency) if row.avg_latency else 0,
        "p95_latency_ms": p95_latency,
        "total_tokens_in": row.total_tokens_in or 0,
        "total_tokens_out": row.total_tokens_out or 0,
        "by_process": by_process,
        "by_model": by_model,
    }
