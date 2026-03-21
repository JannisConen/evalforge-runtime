"""Pipeline: module loading, three-step execution, process chaining."""

from __future__ import annotations

import importlib
import logging
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from evalforge_runtime.config import AppConfig, ProcessConfig
from evalforge_runtime.db import (
    create_execution,
    get_execution,
    get_session_factory,
    list_executions,
    update_execution,
)
from evalforge_runtime.executor import Executor
from evalforge_runtime.storage import LocalStorage
from evalforge_runtime.types import (
    After,
    Before,
    Execution,
    ExecutionContext,
    LLMClient,
    TriggerContext,
)

logger = logging.getLogger(__name__)


class Pipeline:
    """Orchestrates three-step execution and process chaining."""

    def __init__(
        self,
        config: AppConfig,
        executor: Executor,
        storage: LocalStorage,
        secrets: dict[str, str],
    ):
        self.config = config
        self.executor = executor
        self.storage = storage
        self.secrets = secrets
        self._downstream_map: dict[str, list[str]] = {}
        self._modules: dict[str, dict[str, Any]] = {}
        self._build_downstream_map()

    def _build_downstream_map(self) -> None:
        """Build map of process_name -> list of downstream process names."""
        for name, proc in self.config.processes.items():
            if proc.trigger.type == "process" and proc.trigger.after:
                upstream = proc.trigger.after
                self._downstream_map.setdefault(upstream, []).append(name)

    def discover_modules(self) -> None:
        """Discover before/execution/after modules for all processes."""
        for name, proc in self.config.processes.items():
            self._modules[name] = _load_process_modules(name, proc)

    async def execute_process(
        self,
        process_name: str,
        input_data: dict[str, Any],
        trigger: TriggerContext,
        execution_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Execute a single process through the full pipeline.

        Returns the output dict, or None if the execution was skipped/errored.
        """
        proc_config = self.config.processes.get(process_name)
        if not proc_config:
            raise ValueError(f"Unknown process: {process_name}")

        execution_id = execution_id or str(uuid4())
        modules = self._modules.get(process_name, {})
        from evalforge_runtime import __version__

        session_factory = get_session_factory()

        # Create execution record
        async with session_factory() as session:
            await create_execution(
                session,
                execution_id=execution_id,
                process_name=process_name,
                process_id=proc_config.process_id,
                trigger_type=trigger.type,
                trigger_ref=trigger.ref,
                input_data=input_data,
                runtime_version=__version__,
                config_version=self.config.project.version,
            )
            if trigger.source_execution_id:
                await update_execution(
                    session,
                    execution_id,
                    source_execution_id=trigger.source_execution_id,
                )

        logger.info(
            "Starting pipeline for '%s' (execution=%s), input_data=%s",
            process_name, execution_id, input_data,
        )
        start = time.monotonic()

        try:
            # --- EXECUTION step ---
            output, llm_tracking = await self._run_execution(
                process_name, proc_config, modules, input_data, trigger, execution_id
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "Execution step done for '%s' in %dms, output=%s",
                process_name, duration_ms, output,
            )

            # Store LLM tracking data
            if llm_tracking:
                async with session_factory() as session:
                    await update_execution(session, execution_id, **llm_tracking)

            # Check review
            if proc_config.review.enabled:
                async with session_factory() as session:
                    await update_execution(
                        session,
                        execution_id,
                        output=output,
                        status="pending_review",
                        duration_ms=duration_ms,
                    )
                logger.info(
                    f"Execution {execution_id} for '{process_name}' pending review"
                )
                return output

            # --- AFTER step ---
            after_mod = modules.get("after")
            if after_mod:
                logger.info(
                    "Running after step for '%s', trigger=%s, output=%s",
                    process_name, trigger, output,
                )
                try:
                    # Inject runtime context for ActionRunner-based after modules
                    after_mod.config["secrets"] = self.secrets
                    after_mod.config["session_factory"] = session_factory
                    after_mod.config["execution_id"] = execution_id
                    after_mod.config["context"] = {
                        "initiator": proc_config.connector_params or {},
                        "process": {"name": process_name, "id": proc_config.process_id},
                    }
                    await after_mod.execute(trigger, output)
                    logger.info("After step completed for '%s'", process_name)
                except Exception as e:
                    logger.error(f"After step failed for '{process_name}': {e}")
                    # After errors are logged but don't fail the execution

            # Finalize
            async with session_factory() as session:
                await update_execution(
                    session,
                    execution_id,
                    output=output,
                    status="success",
                    finished_at=datetime.utcnow(),
                    duration_ms=duration_ms,
                )

            # Trigger downstream processes
            await self._trigger_downstream(
                process_name, input_data, output, execution_id
            )

            return output

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
            logger.error(f"Execution failed for '{process_name}': {exc}")
            raise

    async def _run_execution(
        self,
        process_name: str,
        proc_config: ProcessConfig,
        modules: dict[str, Any],
        input_data: dict[str, Any],
        trigger: TriggerContext,
        execution_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Run the execution step (custom or default LLM).

        Returns:
            Tuple of (output_dict, llm_tracking_dict_or_None).
        """
        execution_mod: Execution | None = modules.get("execution")

        # Resolve prompts: file-based (prompts/ dir) takes priority, then config fallback
        prompts: dict[str, str] = dict(modules.get("prompts", {}))
        if not prompts.get("system") and proc_config.instructions:
            prompts["system"] = proc_config.instructions
        instructions = prompts.get("system")

        # Resolve output schema: file-based (execution_output_schema.py) takes priority over config
        output_schema_model: type | None = modules.get("output_schema")

        logger.info(
            "Running execution for '%s': custom_module=%s, has_instructions=%s, prompt_count=%d, input=%s",
            process_name, execution_mod is not None, instructions is not None,
            len(prompts), input_data,
        )

        if execution_mod:
            # Custom execution module
            logger.info("Using custom execution module for '%s'", process_name)
            llm_client = LLMClient(proc_config.llm_model or self.config.llm.model)
            context = ExecutionContext(
                llm=llm_client,
                prompts=prompts,
                output_schema=output_schema_model,
                secrets=self.secrets,
                storage=self.storage,
                process_name=process_name,
                process_id=proc_config.process_id,
                trigger=trigger,
            )
            output = await execution_mod.run(input_data, context)
            logger.info("Custom execution output for '%s': %s", process_name, output)

            # Extract LLM tracking from the client (accumulated across all calls)
            llm_tracking = None
            if llm_client.last_model:
                llm_tracking = {
                    "llm_model": llm_client.last_model,
                    "llm_tokens_in": llm_client.total_tokens_in or None,
                    "llm_tokens_out": llm_client.total_tokens_out or None,
                    "llm_cost_usd": llm_client.total_cost_usd or None,
                    "llm_latency_ms": llm_client.total_latency_ms or None,
                }

            return output, llm_tracking
        else:
            # Default LLM execution
            logger.info(
                "Using default LLM execution for '%s', model=%s",
                process_name, proc_config.llm_model or self.config.llm.model,
            )
            if not instructions:
                raise ValueError(
                    f"Process '{process_name}' has no system prompt configured. "
                    "Provide a prompts/system.md file, inline instructions in the config, "
                    "or a custom execution module."
                )

            result = await self.executor.execute(
                instructions=instructions,
                input_data=input_data,
                process_name=process_name,
                model_override=proc_config.llm_model,
                output_schema=output_schema_model or proc_config.output_schema,
            )

            llm_tracking = {
                "llm_model": result.llm_model,
                "llm_tokens_in": result.llm_tokens_in,
                "llm_tokens_out": result.llm_tokens_out,
                "llm_cost_usd": result.llm_cost_usd,
                "llm_latency_ms": result.llm_latency_ms,
                "instructions_version": result.instructions_version,
            }

            return result.output, llm_tracking

    async def _trigger_downstream(
        self,
        source_process: str,
        source_input: dict[str, Any],
        source_output: dict[str, Any],
        source_execution_id: str,
    ) -> None:
        """Trigger downstream processes that chain from this one."""
        downstream = self._downstream_map.get(source_process, [])
        if not downstream:
            return

        logger.info(
            "Triggering %d downstream processes from '%s': %s",
            len(downstream), source_process, downstream,
        )
        source_data = {"input": source_input, "output": source_output}

        for ds_name in downstream:
            ds_modules = self._modules.get(ds_name, {})
            before_mod: Before | None = ds_modules.get("before")

            try:
                # Apply before step if present
                if before_mod:
                    logger.info(
                        "Running before step for downstream '%s', source_data keys=%s",
                        ds_name, list(source_data.keys()),
                    )
                    # Check condition
                    if not before_mod.condition(source_data):
                        logger.info(
                            f"Skipping '{ds_name}': condition returned False"
                        )
                        continue

                    # Prepare input
                    prepared = before_mod.prepare(source_data)
                    if isinstance(prepared, dict):
                        ds_input = prepared
                    else:
                        # Pydantic model
                        ds_input = prepared.model_dump() if hasattr(prepared, "model_dump") else dict(prepared)
                    logger.info("Before step for '%s' prepared input: %s", ds_name, ds_input)
                else:
                    # No before module — pass source output as input
                    ds_input = source_output

                trigger = TriggerContext(
                    type="process_chain",
                    ref=source_process,
                    source_execution_id=source_execution_id,
                )

                await self.execute_process(ds_name, ds_input, trigger)

            except Exception as e:
                logger.error(
                    f"Failed to trigger downstream '{ds_name}' from "
                    f"'{source_process}': {e}"
                )

    async def approve_review(
        self,
        execution_id: str,
        modified_output: dict[str, Any] | None = None,
        reviewed_by: str | None = None,
    ) -> dict[str, Any]:
        """Approve a pending review and run the after step."""
        session_factory = get_session_factory()

        async with session_factory() as session:
            execution = await get_execution(session, execution_id)
            if not execution:
                raise ValueError(f"Execution not found: {execution_id}")
            if execution.status != "pending_review":
                raise ValueError(
                    f"Execution {execution_id} is not pending review "
                    f"(status: {execution.status})"
                )

            import json as _json
            output = modified_output or _json.loads(execution.output_data)
            process_name = execution.process_name
            trigger_ref = execution.trigger_ref
            source_execution_id = execution.source_execution_id
            input_data = _json.loads(execution.input_data)

            await update_execution(
                session,
                execution_id,
                output=output,
                status="success",
                finished_at=datetime.utcnow(),
                reviewed_by=reviewed_by,
                reviewed_at=datetime.utcnow(),
                review_modified=modified_output is not None,
            )

        # Run after step
        modules = self._modules.get(process_name, {})
        after_mod: After | None = modules.get("after")
        if after_mod:
            trigger = TriggerContext(
                type="review_approved",
                ref=trigger_ref,
                source_execution_id=source_execution_id,
            )
            try:
                after_mod.config["secrets"] = self.secrets
                after_mod.config["session_factory"] = get_session_factory()
                after_mod.config["execution_id"] = execution_id
                after_mod.config["context"] = {
                    "initiator": proc_config.connector_params or {},
                    "process": {"name": process_name, "id": proc_config.process_id},
                }
                await after_mod.execute(trigger, output)
            except Exception as e:
                logger.error(f"After step failed for '{process_name}': {e}")

        # Trigger downstream
        await self._trigger_downstream(
            process_name, input_data, output, execution_id
        )

        return output

    async def reject_review(
        self,
        execution_id: str,
        reason: str | None = None,
        reviewed_by: str | None = None,
    ) -> None:
        """Reject a pending review."""
        session_factory = get_session_factory()

        async with session_factory() as session:
            execution = await get_execution(session, execution_id)
            if not execution:
                raise ValueError(f"Execution not found: {execution_id}")
            if execution.status != "pending_review":
                raise ValueError(
                    f"Execution {execution_id} is not pending review "
                    f"(status: {execution.status})"
                )

            await update_execution(
                session,
                execution_id,
                status="rejected",
                error=reason,
                finished_at=datetime.utcnow(),
                reviewed_by=reviewed_by,
                reviewed_at=datetime.utcnow(),
            )

    async def expire_reviews(self) -> int:
        """Expire timed-out pending reviews. Returns count of expired."""
        from evalforge_runtime.config import _parse_duration

        session_factory = get_session_factory()
        expired_count = 0

        async with session_factory() as session:
            pending = await list_executions(
                session, status=["pending_review"], limit=1000
            )

            now = datetime.utcnow()
            for execution in pending:
                proc_config = self.config.processes.get(execution.process_name)
                if not proc_config or not proc_config.review.enabled:
                    continue

                timeout_seconds = _parse_duration(proc_config.review.timeout)
                if execution.started_at and (now - execution.started_at).total_seconds() > timeout_seconds:
                    await update_execution(
                        session,
                        execution.id,
                        status="timed_out",
                        finished_at=now,
                    )
                    expired_count += 1
                    logger.info(
                        f"Review timed out for execution {execution.id} "
                        f"(process: {execution.process_name})"
                    )

        return expired_count


def _load_process_modules(
    process_name: str, process_config: ProcessConfig
) -> dict[str, Any]:
    """Load before/execution/after modules, instructions, and output schema for a process.

    Convention: process named 'email-categorizer' maps to
    'processes.email_categorizer.{before,execution,after,instructions,output_schema}'.
    """
    modules: dict[str, Any] = {}
    module_base = process_name.replace("-", "_")

    for step, cls_type in [("before", Before), ("execution", Execution), ("after", After)]:
        # Check explicit override first
        explicit = getattr(process_config, f"{step}_module", None)
        module_path = explicit or f"processes.{module_base}.{step}"

        try:
            mod = importlib.import_module(module_path)

            # Find the first subclass of the expected base class
            instance = None
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, cls_type)
                    and attr is not cls_type
                ):
                    instance = attr()
                    break

            if instance:
                modules[step] = instance
                logger.info(f"Loaded {step} module for '{process_name}': {module_path}")

        except ImportError:
            # Module not found — that's fine for optional steps
            pass
        except Exception as e:
            logger.error(f"Error loading {step} module for '{process_name}': {e}")

    # Load prompts from prompts/ directory (or legacy instructions.md)
    prompts = _load_prompts(module_base)
    if prompts:
        modules["prompts"] = prompts

    # Load output schema Pydantic model
    output_schema_model = _load_output_schema(module_base)
    if output_schema_model:
        modules["output_schema"] = output_schema_model

    return modules


def _load_prompts(module_base: str) -> dict[str, str]:
    """Load all .md files from processes/{name}/prompts/ as named prompts.

    Falls back to legacy instructions.md for backward compatibility.
    """
    from pathlib import Path

    prompts: dict[str, str] = {}

    # Try prompts/ directory first
    for base in [Path.cwd(), Path(".")]:
        prompts_dir = base / "processes" / module_base / "prompts"
        if prompts_dir.is_dir():
            for f in sorted(prompts_dir.glob("*.md")):
                text = f.read_text(encoding="utf-8").strip()
                if text:
                    prompts[f.stem] = text
            if prompts:
                logger.info(f"Loaded {len(prompts)} prompt(s) from {prompts_dir}: {list(prompts.keys())}")
                return prompts

    # Backward compat: try legacy instructions.md
    for base in [Path.cwd(), Path(".")]:
        old_path = base / "processes" / module_base / "instructions.md"
        if old_path.exists():
            text = old_path.read_text(encoding="utf-8").strip()
            if text:
                logger.info(f"Loaded legacy instructions from {old_path} as prompts['system']")
                return {"system": text}

    return {}


def _load_output_schema(module_base: str) -> type | None:
    """Import OutputSchema from processes/{name}/execution_output_schema.py."""
    module_path = f"processes.{module_base}.execution_output_schema"
    try:
        mod = importlib.import_module(module_path)
        schema_cls = getattr(mod, "OutputSchema", None)
        if schema_cls is not None:
            logger.info(f"Loaded output schema from {module_path}")
            return schema_cls
    except ImportError:
        pass
    except Exception as e:
        logger.error(f"Error loading output schema for '{module_base}': {e}")
    return None


def _parse_duration(duration_str: str) -> int:
    """Parse duration string like '24h', '30m', '7d' to seconds."""
    duration_str = duration_str.strip().lower()
    if duration_str.endswith("d"):
        return int(duration_str[:-1]) * 86400
    elif duration_str.endswith("h"):
        return int(duration_str[:-1]) * 3600
    elif duration_str.endswith("m"):
        return int(duration_str[:-1]) * 60
    elif duration_str.endswith("s"):
        return int(duration_str[:-1])
    else:
        return int(duration_str)
