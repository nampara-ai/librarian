"""Regression tests for Tier 2 run-lifecycle and streaming fixes."""

# These tests intentionally exercise internal helpers (SSE framing, the worker
# fan-out, the event-stream drain loop) that carry no public wrapper.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

import librarian.api.app as api_app
from librarian.api.app import _sse_data_frame, _stream_run_events
from librarian.application.clean_chunks import _run_workers
from librarian.application.jobs import QueueWorker
from librarian.domain.models import RunStatus


def test_sse_data_frame_splits_embedded_newlines() -> None:
    # A raw newline in a data: payload would terminate the SSE event early;
    # each line must become its own data: field.
    frame = _sse_data_frame("line one\nline two")
    assert frame == "data: line one\ndata: line two\n\n"


def test_sse_data_frame_single_line() -> None:
    assert _sse_data_frame("hello") == "data: hello\n\n"


@pytest.mark.asyncio
async def test_run_workers_cancels_siblings_on_first_failure() -> None:
    started = 0
    cancelled = 0

    async def worker() -> None:
        nonlocal started, cancelled
        index = started
        started += 1
        if index == 0:
            # First worker fails fast.
            raise ValueError("boom")
        try:
            # Siblings would otherwise keep running; they must be cancelled.
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled += 1
            raise

    with pytest.raises(ValueError, match="boom"):
        await _run_workers(worker, worker_count=3)

    # The original exception type propagates unwrapped (no ExceptionGroup),
    # and the two still-running siblings were cancelled rather than orphaned.
    assert cancelled == 2


@pytest.mark.asyncio
async def test_run_workers_completes_when_all_succeed() -> None:
    completed = 0

    async def worker() -> None:
        nonlocal completed
        completed += 1

    await _run_workers(worker, worker_count=4)
    assert completed == 4


@pytest.mark.asyncio
async def test_run_forever_survives_transient_claim_error() -> None:
    calls = 0

    class FlakyQueue:
        async def claim(self, *, worker_id: str, lease_seconds: int) -> None:
            nonlocal calls
            del worker_id, lease_seconds
            calls += 1
            if calls == 1:
                raise RuntimeError("transient DB error")
            # Second poll: signal the loop to stop and report no work.
            worker.stop()
            return None

    async def processor(run_id: object) -> object:
        del run_id
        return None

    worker = QueueWorker(
        queue=FlakyQueue(),  # type: ignore[arg-type]
        processor=processor,
        worker_id="w1",
        poll_interval_seconds=0.0,
    )

    # A transient error in the first claim must not kill the worker loop; it
    # backs off and polls again, at which point we stop it.
    await asyncio.wait_for(worker.run_forever(), timeout=2.0)
    assert calls == 2


@pytest.mark.asyncio
async def test_event_stream_drains_tail_events_after_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TerminalRun:
        status = RunStatus.SUCCEEDED

    class _Repo:
        def __init__(self) -> None:
            # e1 is visible on the first poll; e2 lands after the run is already
            # terminal (the pipeline emits its final "complete" event *after*
            # writing the terminal status), so only the drain can recover it.
            self._events = ["e1", "e2-after-terminal"]

        async def list_events(self, offset: int) -> list[str]:
            return self._events[offset:]

        async def get_run(self, run_id: object) -> _TerminalRun:
            del run_id
            return _TerminalRun()

    repo = _Repo()

    class _Container:
        repository = repo

    async def _fake_container(settings: object) -> _Container:
        del settings
        return _Container()

    async def fetch_frames(container: Any, offset: int) -> list[str]:
        del container
        events = await repo.list_events(offset)
        return [_sse_data_frame(event) for event in events]

    monkeypatch.setattr(api_app, "build_ingest_container", _fake_container)

    frames = [
        frame
        async for frame in _stream_run_events(
            settings=cast("Any", None),
            run_id=cast("Any", "run_x"),
            fetch_frames=fetch_frames,
        )
    ]

    joined = "".join(frames)
    assert "data: e1" in joined
    assert "data: e2-after-terminal" in joined
    assert joined.endswith("event: done\ndata: done\n\n")
