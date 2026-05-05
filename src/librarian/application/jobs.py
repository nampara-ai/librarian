"""Job execution adapters for long-running processing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from librarian.domain.ids import RunId

JobFactory = Callable[[], Awaitable[object]]
RunProcessor = Callable[[RunId], Awaitable[object]]


class QueueStatus(StrEnum):
    """Durable worker queue states."""

    QUEUED = "queued"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class QueuedRun:
    """A durable processing queue item."""

    run_id: RunId
    status: QueueStatus
    attempts: int
    available_at: datetime
    locked_at: datetime | None = None
    locked_by: str | None = None
    last_error: str | None = None


class RunQueue(Protocol):
    """Port for durable run queue adapters."""

    async def enqueue(self, run_id: RunId) -> None: ...

    async def claim(self, *, worker_id: str, lease_seconds: int) -> QueuedRun | None: ...

    async def complete(self, run_id: RunId) -> None: ...

    async def fail(
        self,
        run_id: RunId,
        *,
        error: str,
        max_attempts: int,
    ) -> None: ...


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


@dataclass(slots=True)
class QueueWorker:
    """Worker loop for durable queues."""

    queue: RunQueue
    processor: RunProcessor
    worker_id: str
    lease_seconds: int = 300
    max_attempts: int = 3
    poll_interval_seconds: float = 1.0
    _stopping: bool = False

    def stop(self) -> None:
        """Request a graceful stop after the current item."""
        self._stopping = True

    async def run_once(self) -> bool:
        """Claim and process one queued run. Returns true when work was done."""
        item = await self.queue.claim(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if item is None:
            return False

        try:
            await self.processor(item.run_id)
        except Exception as exc:
            await self.queue.fail(
                item.run_id,
                error=str(exc),
                max_attempts=self.max_attempts,
            )
            raise
        await self.queue.complete(item.run_id)
        return True

    async def run_forever(self) -> None:
        """Poll the queue until stopped."""
        while not self._stopping:
            did_work = await self.run_once()
            if not did_work:
                await asyncio.sleep(self.poll_interval_seconds)
