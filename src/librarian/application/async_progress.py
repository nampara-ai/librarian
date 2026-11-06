"""Heartbeat long operations and cancel their children when the caller stops."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


async def await_with_progress[T](
    operation: Awaitable[T],
    on_wait: Callable[[float], Awaitable[None]],
    *,
    interval_seconds: float = 10.0,
) -> T:
    task = asyncio.ensure_future(operation)
    started = time.monotonic()
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=interval_seconds)
            if not done:
                await on_wait(time.monotonic() - started)
        return await task
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
