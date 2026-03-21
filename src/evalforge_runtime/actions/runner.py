"""ActionRunner — sequential action executor with condition evaluation and logging."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from evalforge_runtime.actions.base import BaseAction
from evalforge_runtime.condition import evaluate_condition

logger = logging.getLogger(__name__)


class ActionRunner:
    """Sequential action executor with built-in condition evaluation and logging."""

    def __init__(self) -> None:
        self._actions: list[tuple[BaseAction, dict[str, Any]]] = []

    def add(
        self,
        action: BaseAction,
        condition: dict[str, Any] | None = None,
    ) -> None:
        """Register an action with its condition."""
        self._actions.append((action, condition or {"mode": "always"}))

    async def execute_all(
        self,
        trigger: Any,
        output: dict[str, Any],
        *,
        secrets: dict[str, str] | None = None,
        context: dict[str, Any] | None = None,
        session_factory: Any | None = None,
        execution_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute all registered actions sequentially.

        Returns a list of result dicts with status/timing for each action.
        """
        results: list[dict[str, Any]] = []
        _secrets = secrets or {}
        _context = context or {}

        action_types = [a.type for a, _ in self._actions]
        logger.info(
            "ActionRunner.execute_all: execution_id=%s, trigger=%s, %d actions=%s, output=%s",
            execution_id, trigger, len(self._actions), action_types, output,
        )

        for i, (action, condition) in enumerate(self._actions):
            start = time.monotonic()
            status = "success"
            error: str | None = None

            if not self._evaluate_condition(condition, output):
                status = "skipped"
                duration_ms = int((time.monotonic() - start) * 1000)
                logger.info(
                    "Action [%d] %s: SKIPPED (condition=%s)",
                    i, action.type, condition.get("mode", "always"),
                )
                result = {
                    "action_type": action.type,
                    "action_index": i,
                    "status": status,
                    "duration_ms": duration_ms,
                }
                results.append(result)
                await self._log(
                    session_factory, execution_id, action, i, status, start, duration_ms
                )
                continue

            logger.info(
                "Action [%d] %s: RUNNING (config=%s)",
                i, action.type, action.config,
            )
            try:
                await action.run(trigger=trigger, output=output, secrets=_secrets, context=_context)
            except Exception as e:
                status = "error"
                error = str(e)
                logger.error("Action [%d] %s: FAILED (%dms): %s", i, action.type, int((time.monotonic() - start) * 1000), e)
                # Continue to next action — don't fail the pipeline

            duration_ms = int((time.monotonic() - start) * 1000)
            if status == "success":
                logger.info("Action [%d] %s: SUCCESS (%dms)", i, action.type, duration_ms)

            result = {
                "action_type": action.type,
                "action_index": i,
                "status": status,
                "duration_ms": duration_ms,
                "error": error,
            }
            results.append(result)
            await self._log(
                session_factory,
                execution_id,
                action,
                i,
                status,
                start,
                duration_ms,
                error,
            )

        logger.info(
            "ActionRunner.execute_all: DONE, results=%s",
            [(r["action_type"], r["status"], r["duration_ms"]) for r in results],
        )
        return results

    # ------------------------------------------------------------------
    # Condition evaluation (delegates to shared module)
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_condition(
        condition: dict[str, Any], output: dict[str, Any]
    ) -> bool:
        return evaluate_condition(condition, output, fn_name="should_run")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    async def _log(
        self,
        session_factory: Any | None,
        execution_id: str | None,
        action: BaseAction,
        index: int,
        status: str,
        start_time: float,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        """Log action execution to the ActionLog table."""
        if session_factory is None or execution_id is None:
            return

        try:
            from evalforge_runtime.db import ActionLog

            async with session_factory() as session:
                log = ActionLog(
                    id=str(uuid4()),
                    execution_id=execution_id,
                    action_type=action.type,
                    action_index=index,
                    status=status,
                    started_at=datetime.utcfromtimestamp(start_time),
                    finished_at=datetime.utcnow(),
                    duration_ms=duration_ms,
                    error=error,
                )
                session.add(log)
                await session.commit()
        except Exception as e:
            logger.warning(f"Failed to log action execution: {e}")
