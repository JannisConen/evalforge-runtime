"""SQLAlchemy execution model and database management."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# JSON column — use Text and serialize manually for SQLite compat
import json as _json


class Base(DeclarativeBase):
    pass


class ExecutionRecord(Base):
    __tablename__ = "executions"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    process_name = Column(String, nullable=False)
    process_id = Column(String, nullable=False)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    trigger_type = Column(String, nullable=False)
    trigger_ref = Column(String, nullable=True)
    source_execution_id = Column(String, nullable=True)
    input_data = Column(Text, nullable=False)  # JSON string
    output_data = Column(Text, nullable=True)  # JSON string
    status = Column(String, nullable=False, default="pending")
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    # LLM tracking
    llm_model = Column(String, nullable=True)
    llm_tokens_in = Column(Integer, nullable=True)
    llm_tokens_out = Column(Integer, nullable=True)
    llm_cost_usd = Column(Float, nullable=True)
    llm_latency_ms = Column(Integer, nullable=True)
    # Versioning
    instructions_version = Column(String, nullable=True)
    runtime_version = Column(String, nullable=True)
    config_version = Column(String, nullable=True)
    # Review (Phase 1: unused, forward compat)
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_modified = Column(Boolean, nullable=True)

    __table_args__ = (
        Index("idx_executions_process", "process_name", started_at.desc()),
        Index("idx_executions_trigger_ref", "process_name", "trigger_ref"),
        Index("idx_executions_status", "status"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to API response dict."""
        return {
            "id": self.id,
            "process_name": self.process_name,
            "process_id": self.process_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "trigger_type": self.trigger_type,
            "trigger_ref": self.trigger_ref,
            "source_execution_id": self.source_execution_id,
            "input": _json.loads(self.input_data) if self.input_data else None,
            "output": _json.loads(self.output_data) if self.output_data else None,
            "status": self.status,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "llm_model": self.llm_model,
            "llm_tokens_in": self.llm_tokens_in,
            "llm_tokens_out": self.llm_tokens_out,
            "llm_cost_usd": self.llm_cost_usd,
            "llm_latency_ms": self.llm_latency_ms,
            "instructions_version": self.instructions_version,
            "runtime_version": self.runtime_version,
            "config_version": self.config_version,
        }


class ActionLog(Base):
    """Log of each action execution within a process run."""

    __tablename__ = "action_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    execution_id = Column(String, nullable=False, index=True)
    action_type = Column(String, nullable=False)
    action_index = Column(Integer, nullable=False)
    status = Column(String, nullable=False)  # "success", "error", "skipped"
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    action_metadata = Column("metadata", Text, nullable=True)  # JSON


# Global engine and session factory
_engine = None
_session_factory = None


async def init_db(database_url: str) -> None:
    """Initialize the database engine and create tables."""
    global _engine, _session_factory

    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close the database engine."""
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get the session factory. Must call init_db first."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


async def create_execution(
    session: AsyncSession,
    *,
    execution_id: str,
    process_name: str,
    process_id: str,
    trigger_type: str,
    input_data: dict[str, Any],
    trigger_ref: str | None = None,
    runtime_version: str | None = None,
    config_version: str | None = None,
) -> ExecutionRecord:
    """Create a new execution record."""
    record = ExecutionRecord(
        id=execution_id,
        process_name=process_name,
        process_id=process_id,
        started_at=datetime.utcnow(),
        trigger_type=trigger_type,
        trigger_ref=trigger_ref,
        input_data=_json.dumps(input_data),
        status="running",
        runtime_version=runtime_version,
        config_version=config_version,
    )
    session.add(record)
    await session.commit()
    return record


async def update_execution(
    session: AsyncSession,
    execution_id: str,
    **kwargs: Any,
) -> None:
    """Update fields on an execution record."""
    result = await session.execute(
        select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        return

    for key, value in kwargs.items():
        if key == "output":
            setattr(record, "output_data", _json.dumps(value) if value is not None else None)
        elif key == "input":
            setattr(record, "input_data", _json.dumps(value))
        else:
            setattr(record, key, value)

    await session.commit()


async def get_execution(session: AsyncSession, execution_id: str) -> ExecutionRecord | None:
    """Get a single execution by ID."""
    result = await session.execute(
        select(ExecutionRecord).where(ExecutionRecord.id == execution_id)
    )
    return result.scalar_one_or_none()


async def list_executions(
    session: AsyncSession,
    *,
    process_name: str | None = None,
    status: list[str] | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ExecutionRecord]:
    """List executions with optional filters."""
    query = select(ExecutionRecord).order_by(ExecutionRecord.started_at.desc())

    if process_name:
        query = query.where(ExecutionRecord.process_name == process_name)
    if status:
        query = query.where(ExecutionRecord.status.in_(status))
    if from_date:
        query = query.where(ExecutionRecord.started_at >= from_date)
    if to_date:
        query = query.where(ExecutionRecord.started_at <= to_date)

    query = query.limit(limit).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def trigger_ref_exists(
    session: AsyncSession, process_name: str, trigger_ref: str
) -> bool:
    """Check if a trigger_ref has already been processed for a given process."""
    result = await session.execute(
        select(ExecutionRecord.id)
        .where(
            ExecutionRecord.process_name == process_name,
            ExecutionRecord.trigger_ref == trigger_ref,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def get_last_execution_time(
    session: AsyncSession, process_name: str
) -> datetime | None:
    """Get the timestamp of the most recent execution for a process."""
    result = await session.execute(
        select(ExecutionRecord.started_at)
        .where(ExecutionRecord.process_name == process_name)
        .order_by(ExecutionRecord.started_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row
