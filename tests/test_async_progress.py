import asyncio

import pytest

from librarian.application.async_progress import await_with_progress
from librarian.application.errors import ProcessingCanceled


@pytest.mark.asyncio
async def test_heartbeat_preserves_result_and_reports_waiting() -> None:
    release = asyncio.Event()
    ticks: list[float] = []

    async def operation() -> str:
        await release.wait()
        return "finished"

    async def heartbeat(elapsed: float) -> None:
        ticks.append(elapsed)
        release.set()

    result = await await_with_progress(operation(), heartbeat, interval_seconds=0.001)
    assert result == "finished"
    assert ticks and ticks[0] > 0


@pytest.mark.asyncio
async def test_cancel_from_heartbeat_awaits_child_cleanup() -> None:
    stopped = asyncio.Event()

    async def operation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def heartbeat(_: float) -> None:
        raise ProcessingCanceled("Canceled by user")

    with pytest.raises(ProcessingCanceled):
        await await_with_progress(operation(), heartbeat, interval_seconds=0.001)
    assert stopped.is_set()
