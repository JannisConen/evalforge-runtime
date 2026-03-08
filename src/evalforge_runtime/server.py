"""FastAPI application factory and route registration."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile

from evalforge_runtime import __version__
from evalforge_runtime.auth import APIKeyAuth
from evalforge_runtime.config import AppConfig, ProcessConfig
from evalforge_runtime.db import (
    close_db,
    create_execution,
    get_execution,
    get_last_execution_time,
    get_session_factory,
    init_db,
    list_executions,
    update_execution,
)
from evalforge_runtime.executor import Executor
from evalforge_runtime.files import process_uploaded_file, resolve_file_refs
from evalforge_runtime.observability import get_execution_stats
from evalforge_runtime.pipeline import Pipeline
from evalforge_runtime.scheduler import Scheduler
from evalforge_runtime.secrets import SecretManager
from evalforge_runtime.storage import LocalStorage

logger = logging.getLogger(__name__)

_start_time: float = 0.0


def create_app(config: AppConfig) -> FastAPI:
    """Create and configure the FastAPI application."""

    # Resolve database URL
    db_url = config.database.url

    # Initialize subsystems
    storage = LocalStorage(config.storage.path)
    executor = Executor(config.llm.model, observability=config.observability)
    auth_dep = APIKeyAuth(config.auth)
    scheduler = Scheduler()
    secret_manager = SecretManager(
        config.secrets,
        project_id=config.project.id,
        evalforge_url=config.project.evalforge_url or "",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _start_time
        _start_time = time.monotonic()

        # Init DB
        await init_db(db_url)

        # Load secrets
        secrets = await secret_manager.load()

        # Initialize connectors
        connectors = _init_connectors(config, secrets, storage)
        for cname, conn in connectors.items():
            try:
                await conn.validate()
                logger.info(f"Connector '{cname}' validated")
            except Exception as e:
                logger.error(f"Connector '{cname}' validation failed: {e}")

        # Initialize pipeline
        pipeline = Pipeline(config, executor, storage, secrets)
        pipeline.discover_modules()
        app.state.pipeline = pipeline

        # Start scheduler and register cron jobs
        await scheduler.start()
        _register_scheduled_jobs(config, scheduler, pipeline, connectors)

        # Register review expiration job (every minute)
        if any(p.review.enabled for p in config.processes.values()):
            scheduler.add_cron_job(
                job_id="_review_expiration",
                cron_expression="* * * * *",
                func=pipeline.expire_reviews,
            )

        yield

        await scheduler.stop()
        await close_db()

    app = FastAPI(
        title="EvalForge Runtime",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.pipeline = None  # Set during lifespan, or injected in tests

    # --- Health endpoint (no auth) ---

    @app.get("/health")
    async def health() -> dict[str, Any]:
        uptime = int(time.monotonic() - _start_time) if _start_time else 0

        processes_status: dict[str, Any] = {}
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                for pname in config.processes:
                    last = await get_last_execution_time(session, pname)
                    processes_status[pname] = {
                        "status": "active",
                        "last_execution": last.isoformat() if last else None,
                    }
        except Exception:
            for pname in config.processes:
                processes_status[pname] = {"status": "active", "last_execution": None}

        return {
            "status": "healthy",
            "project_id": config.project.id,
            "config_version": config.project.version,
            "runtime_version": __version__,
            "uptime_seconds": uptime,
            "processes": processes_status,
        }

    # --- Process endpoints (authenticated) ---

    for process_name, process_config in config.processes.items():
        _register_process_route(
            app, process_name, process_config, executor, storage, auth_dep, config,
            lambda: app.state.pipeline,
        )

    # --- Execution endpoints (authenticated) ---

    @app.get("/executions")
    async def list_executions_endpoint(
        process: str | None = None,
        status: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 50,
        offset: int = 0,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        status_list = [s.strip() for s in status.split(",")] if status else None
        from_dt = datetime.fromisoformat(from_date) if from_date else None
        to_dt = datetime.fromisoformat(to_date) if to_date else None

        session_factory = get_session_factory()
        async with session_factory() as session:
            executions = await list_executions(
                session,
                process_name=process,
                status=status_list,
                from_date=from_dt,
                to_date=to_dt,
                limit=limit,
                offset=offset,
            )
            return {
                "executions": [e.to_dict() for e in executions],
                "limit": limit,
                "offset": offset,
            }

    @app.get("/executions/stats")
    async def execution_stats_endpoint(
        process: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        from_dt = datetime.fromisoformat(from_date) if from_date else None
        to_dt = datetime.fromisoformat(to_date) if to_date else None

        session_factory = get_session_factory()
        async with session_factory() as session:
            return await get_execution_stats(
                session,
                process_name=process,
                from_date=from_dt,
                to_date=to_dt,
            )

    @app.get("/executions/{execution_id}")
    async def get_execution_endpoint(
        execution_id: str,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        session_factory = get_session_factory()
        async with session_factory() as session:
            execution = await get_execution(session, execution_id)
            if execution is None:
                raise HTTPException(status_code=404, detail="Execution not found")
            return execution.to_dict()

    # --- Review endpoints (authenticated) ---

    @app.get("/reviews")
    async def list_reviews_endpoint(
        process: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        review_status = status or "pending_review"
        status_list = [s.strip() for s in review_status.split(",")]

        session_factory = get_session_factory()
        async with session_factory() as session:
            executions = await list_executions(
                session,
                process_name=process,
                status=status_list,
                limit=limit,
                offset=offset,
            )
            return {
                "reviews": [e.to_dict() for e in executions],
                "limit": limit,
                "offset": offset,
            }

    @app.get("/reviews/{execution_id}")
    async def get_review_endpoint(
        execution_id: str,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        session_factory = get_session_factory()
        async with session_factory() as session:
            execution = await get_execution(session, execution_id)
            if execution is None:
                raise HTTPException(status_code=404, detail="Execution not found")

            proc_config = config.processes.get(execution.process_name)
            result = execution.to_dict()
            result["timeout"] = proc_config.review.timeout if proc_config else "24h"
            return result

    @app.post("/reviews/{execution_id}/approve")
    async def approve_review_endpoint(
        execution_id: str,
        request: Request,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        pl = app.state.pipeline
        if not pl:
            raise HTTPException(status_code=503, detail="Pipeline not initialized")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass

        modified_output = body.get("output")
        reviewed_by = body.get("reviewed_by")

        try:
            output = await pl.approve_review(
                execution_id, modified_output, reviewed_by
            )
            return {"status": "approved", "output": output}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/reviews/{execution_id}/reject")
    async def reject_review_endpoint(
        execution_id: str,
        request: Request,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        pl = app.state.pipeline
        if not pl:
            raise HTTPException(status_code=503, detail="Pipeline not initialized")

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass

        reason = body.get("reason")
        reviewed_by = body.get("reviewed_by")

        try:
            await pl.reject_review(execution_id, reason, reviewed_by)
            return {"status": "rejected"}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    return app


def _make_process_handler(
    process_name: str,
    process_config: ProcessConfig,
    executor: Executor,
    storage: LocalStorage,
    auth_dep: APIKeyAuth,
    config: AppConfig,
    get_pipeline,
):
    """Create a process endpoint handler with properly captured closure variables."""

    async def process_endpoint(
        request: Request,
        _auth: str = Depends(auth_dep),
    ) -> dict[str, Any]:
        execution_id = str(uuid4())
        content_type = request.headers.get("content-type", "")

        # Parse input based on content type
        if "multipart/form-data" in content_type:
            input_data = await _parse_multipart(request, execution_id, storage)
        else:
            try:
                input_data = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid JSON body")

        if not isinstance(input_data, dict):
            raise HTTPException(status_code=400, detail="Input must be a JSON object")

        # Resolve FileRefs: download from URL or decode base64 data
        input_data = await resolve_file_refs(input_data, execution_id, storage)

        # Use pipeline if available
        pl = get_pipeline()
        if pl:
            from evalforge_runtime.types import TriggerContext

            trigger = TriggerContext(type="webhook", ref=execution_id)
            try:
                output = await pl.execute_process(
                    process_name, input_data, trigger, execution_id
                )
                return output or {}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

        # Fallback: direct LLM execution (Phase 1 mode)
        # Load instructions from file first, fall back to config
        from evalforge_runtime.pipeline import _load_instructions, _load_output_schema

        module_base = process_name.replace("-", "_")
        instructions = _load_instructions(module_base) or process_config.instructions
        if not instructions:
            raise HTTPException(
                status_code=501,
                detail="No instructions configured for this process. "
                "Add instructions.md or configure instructions in the EvalForge UI and re-generate.",
            )

        output_schema_model = _load_output_schema(module_base)

        session_factory = get_session_factory()
        async with session_factory() as session:
            await create_execution(
                session,
                execution_id=execution_id,
                process_name=process_name,
                process_id=process_config.process_id,
                trigger_type="webhook",
                input_data=input_data,
                runtime_version=__version__,
                config_version=config.project.version,
            )

        start = time.monotonic()
        try:
            result = await executor.execute(
                instructions=instructions,
                input_data=input_data,
                process_name=process_name,
                model_override=process_config.llm_model,
                output_schema=output_schema_model or process_config.output_schema,
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            async with session_factory() as session:
                await update_execution(
                    session,
                    execution_id,
                    output=result.output,
                    status="success",
                    finished_at=datetime.utcnow(),
                    duration_ms=duration_ms,
                    llm_model=result.llm_model,
                    llm_tokens_in=result.llm_tokens_in,
                    llm_tokens_out=result.llm_tokens_out,
                    llm_cost_usd=result.llm_cost_usd,
                    llm_latency_ms=result.llm_latency_ms,
                    instructions_version=result.instructions_version,
                )

            return result.output

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            async with session_factory() as session:
                await update_execution(
                    session,
                    execution_id,
                    status="error",
                    error=str(exc),
                    finished_at=datetime.utcnow(),
                    duration_ms=duration_ms,
                )
            raise HTTPException(status_code=500, detail=str(exc))

    return process_endpoint


def _register_process_route(
    app: FastAPI,
    process_name: str,
    process_config: ProcessConfig,
    executor: Executor,
    storage: LocalStorage,
    auth_dep: APIKeyAuth,
    config: AppConfig,
    get_pipeline=None,
) -> None:
    """Register a POST /process/{name} endpoint for a process."""
    handler = _make_process_handler(
        process_name, process_config, executor, storage, auth_dep, config,
        get_pipeline or (lambda: None),
    )
    app.add_api_route(
        f"/process/{process_name}",
        handler,
        methods=["POST"],
        name=f"process_{process_name}",
    )


async def _parse_multipart(
    request: Request,
    execution_id: str,
    storage: LocalStorage,
) -> dict[str, Any]:
    """Parse a multipart/form-data request into an input dict with FileRefs."""
    form = await request.form()
    input_data: dict[str, Any] = {}
    file_refs: list[dict[str, Any]] = []

    for field_name, field_value in form.items():
        if isinstance(field_value, UploadFile):
            file_ref = await process_uploaded_file(
                field_value, execution_id, storage
            )
            file_refs.append(file_ref.model_dump(by_alias=True))
        elif field_name == "metadata":
            try:
                metadata = json.loads(field_value) if isinstance(field_value, str) else {}
                input_data.update(metadata)
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            input_data[field_name] = field_value

    if file_refs:
        if len(file_refs) == 1:
            input_data["file"] = file_refs[0]
        else:
            input_data["files"] = file_refs

    return input_data


def _init_connectors(
    config: AppConfig, secrets: dict[str, str], storage: LocalStorage
) -> dict[str, Any]:
    """Initialize connectors for processes that use them."""
    from evalforge_runtime.connectors.exchange import ExchangeConnector
    from evalforge_runtime.connectors.gmail import GmailConnector
    from evalforge_runtime.connectors.webhook import WebhookConnector

    connector_map = {
        "exchange": ExchangeConnector,
        "gmail": GmailConnector,
        "webhook": WebhookConnector,
    }

    connectors: dict[str, Any] = {}
    for pname, pconfig in config.processes.items():
        if pconfig.connector and pconfig.connector in connector_map:
            cls = connector_map[pconfig.connector]
            connectors[pname] = cls(
                params=pconfig.connector_params,
                secrets=secrets,
                storage=storage,
            )
    return connectors


def _register_scheduled_jobs(
    config: AppConfig,
    scheduler: Scheduler,
    pipeline: Pipeline,
    connectors: dict[str, Any],
) -> None:
    """Register cron-triggered jobs for scheduled processes."""
    for pname, pconfig in config.processes.items():
        if pconfig.trigger.type == "schedule" and pconfig.trigger.cron:
            connector = connectors.get(pname)

            async def _scheduled_run(
                _pname: str = pname,
                _connector: Any = connector,
            ) -> None:
                from evalforge_runtime.types import TriggerContext

                if _connector:
                    # Fetch items from connector
                    try:
                        items = await _connector.fetch()
                    except Exception as e:
                        logger.error(f"Connector fetch failed for '{_pname}': {e}")
                        return

                    for item in items:
                        # Deduplication: check if already processed
                        session_factory = get_session_factory()
                        async with session_factory() as session:
                            existing = await list_executions(
                                session,
                                process_name=_pname,
                                limit=1,
                            )
                            # Simple dedup by trigger_ref
                            if existing and any(
                                e.trigger_ref == item.ref for e in existing
                            ):
                                continue

                        trigger = TriggerContext(
                            type="schedule", ref=item.ref
                        )
                        input_data = item.data
                        if item.attachments:
                            input_data["attachments"] = [
                                a.model_dump(by_alias=True) for a in item.attachments
                            ]

                        try:
                            await pipeline.execute_process(
                                _pname, input_data, trigger
                            )
                        except Exception as e:
                            logger.error(
                                f"Scheduled execution failed for '{_pname}' "
                                f"(ref: {item.ref}): {e}"
                            )
                else:
                    # No connector — just trigger with empty input
                    trigger = TriggerContext(type="schedule")
                    try:
                        await pipeline.execute_process(_pname, {}, trigger)
                    except Exception as e:
                        logger.error(f"Scheduled execution failed for '{_pname}': {e}")

            scheduler.add_cron_job(
                job_id=f"process_{pname}",
                cron_expression=pconfig.trigger.cron,
                func=_scheduled_run,
            )


def _create_app_from_env() -> FastAPI | None:
    """Factory for uvicorn: reads EVALFORGE_CONFIG env var to create the app.

    Usage: EVALFORGE_CONFIG=path.yaml uvicorn evalforge_runtime.server:app --reload
    """
    config_path = os.environ.get("EVALFORGE_CONFIG")
    if not config_path:
        return None

    from evalforge_runtime.config import load_config

    config = load_config(config_path)
    return create_app(config)


# Module-level app for uvicorn import (only created when EVALFORGE_CONFIG is set)
app = _create_app_from_env()
