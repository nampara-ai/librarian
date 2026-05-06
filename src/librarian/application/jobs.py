"""Job execution adapters for long-running processing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from librarian.application.process_document import ProcessingCanceled
from librarian.domain.ids import RunId

JobFactory = Callable[[], Awaitable[object]]
RunProcessor = Callable[[RunId], Coroutine[Any, Any, object]]


class QueueStatus(StrEnum):
    """Durable worker queue states."""

    QUEUED = "queued"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


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

    async def heartbeat(self, run_id: RunId, *, worker_id: str, lease_seconds: int) -> bool: ...

    async def complete(self, run_id: RunId, *, worker_id: str | None = None) -> None: ...

    async def fail(
        self,
        run_id: RunId,
        *,
        error: str,
        max_attempts: int,
        worker_id: str | None = None,
    ) -> None: ...

    async def cancel(self, run_id: RunId, *, error: str | None = None) -> None: ...

    async def list(self, *, limit: int = 100) -> tuple[QueuedRun, ...]: ...


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
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("librarian.jobs")
    )
    _semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def submit(self, run_id: RunId, factory: JobFactory) -> None:
        """Submit a job for asynchronous execution."""
        task = asyncio.create_task(self._run(run_id, factory))
        task.add_done_callback(lambda finished: self._observe_task(run_id, finished))
        self._tasks[run_id] = task

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
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception(
                    "in_process_job_failed",
                    extra={"run_id": str(run_id)},
                )
                raise
            finally:
                self._tasks.pop(run_id, None)

    def _observe_task(self, run_id: RunId, task: asyncio.Task[object]) -> None:
        if task.cancelled():
            self.logger.info(
                "in_process_job_cancelled",
                extra={"run_id": str(run_id)},
            )
            return
        _ = task.exception()


@dataclass(slots=True)
class QueueWorker:
    """Worker loop for durable queues."""

    queue: RunQueue
    processor: RunProcessor
    worker_id: str
    lease_seconds: int = 300
    max_attempts: int = 3
    poll_interval_seconds: float = 1.0
    heartbeat_interval_seconds: float | None = None
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
            await self._process_with_heartbeat(item.run_id)
        except ProcessingCanceled as exc:
            await self.queue.cancel(item.run_id, error=str(exc))
            return True
        except Exception as exc:
            await self.queue.fail(
                item.run_id,
                error=str(exc),
                max_attempts=self.max_attempts,
                worker_id=self.worker_id,
            )
            return True
        await self.queue.complete(item.run_id, worker_id=self.worker_id)
        return True

    async def run_forever(self) -> None:
        """Poll the queue until stopped."""
        while not self._stopping:
            did_work = await self.run_once()
            if not did_work:
                await asyncio.sleep(self.poll_interval_seconds)

    async def _process_with_heartbeat(self, run_id: RunId) -> object:
        task: asyncio.Task[object] = asyncio.create_task(self.processor(run_id))
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_until_done(run_id, task, lost_lease))
        try:
            done, _ = await asyncio.wait(
                {task, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                try:
                    heartbeat.result()
                except Exception:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                if not task.done():
                    task.cancel()
                await task
            return await task
        except asyncio.CancelledError as exc:
            if lost_lease.is_set():
                raise RuntimeError(f"Lost queue lease for run {run_id}") from exc
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat_until_done(
        self,
        run_id: RunId,
        processor_task: asyncio.Task[object],
        lost_lease: asyncio.Event,
    ) -> None:
        interval = self.heartbeat_interval_seconds
        if interval is None:
            interval = max(1.0, min(30.0, self.lease_seconds / 3))
        while not processor_task.done():
            await asyncio.sleep(interval)
            if processor_task.done():
                return
            renewed = await self.queue.heartbeat(
                run_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                lost_lease.set()
                processor_task.cancel()
                raise RuntimeError(f"Lost queue lease for run {run_id}")
