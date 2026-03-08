"""APScheduler-based cron trigger registration."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class Scheduler:
    """Manages cron-based process triggers using APScheduler."""

    def __init__(self) -> None:
        self._scheduler: Any = None
        self._jobs: list[str] = []

    async def start(self) -> None:
        """Start the scheduler."""
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            self._scheduler = AsyncIOScheduler()
            self._scheduler.start()
            logger.info("Scheduler started")
        except ImportError:
            logger.warning(
                "APScheduler not installed. Schedule triggers will not work. "
                "Install with: pip install apscheduler"
            )

    async def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def add_cron_job(
        self,
        job_id: str,
        cron_expression: str,
        func: Callable[..., Coroutine[Any, Any, None]],
        **kwargs: Any,
    ) -> None:
        """Add a cron-triggered job.

        Args:
            job_id: Unique identifier for the job
            cron_expression: Cron expression (e.g., "*/5 * * * *")
            func: Async function to call on each trigger
        """
        if not self._scheduler:
            logger.warning(f"Scheduler not available, skipping job '{job_id}'")
            return

        from apscheduler.triggers.cron import CronTrigger

        # Parse standard cron expression: min hour day month weekday
        parts = cron_expression.strip().split()
        if len(parts) == 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        else:
            raise ValueError(
                f"Invalid cron expression '{cron_expression}': expected 5 fields"
            )

        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            kwargs=kwargs,
        )
        self._jobs.append(job_id)
        logger.info(f"Scheduled job '{job_id}' with cron: {cron_expression}")

    @property
    def job_ids(self) -> list[str]:
        return list(self._jobs)
