"""Job execution adapters for long-running processing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from librarian.domain.ids import RunId

JobFactory = Callable[[], Awaitable[object]]


@dataclass(slots=True)
class InProcessJobRunner:
    """Bounded in-process job runner.

    This is intentionally separate from FastAPI BackgroundTasks so API code can
    later swap in a durable external queue without changing route behavior.
    """

    max_concurrency: int = 2
    _tasks: dict[RunId, asyncio.Task[object]] = field(
        default_factory=lambda: dict[RunId, asyncio.Task[object]]()
    )
    _semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def submit(self, run_id: RunId, factory: JobFactory) -> None:
        """Submit a job for asynchronous execution."""
        self._tasks[run_id] = asyncio.create_task(self._run(run_id, factory))

    async def wait(self, run_id: RunId) -> object | None:
        """Wait for a submitted job to finish."""
        task = self._tasks.get(run_id)
        if task is None:
            return None
        return await task

    async def shutdown(self) -> None:
        """Cancel any still-running jobs."""
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, run_id: RunId, factory: JobFactory) -> object:
        async with self._semaphore:
            try:
                return await factory()
            finally:
                self._tasks.pop(run_id, None)
