"""Tests for the parent-process death watcher."""

# The termination hook is internal; the test swaps it to observe the watcher.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import threading

import librarian.runtime.parent_watch as watch_module
from librarian.runtime.parent_watch import (
    PARENT_PID_ENV,
    parent_pid_from_env,
    process_alive,
    start_parent_death_watcher,
)


def test_parent_pid_from_env_parses_valid_pid() -> None:
    assert parent_pid_from_env({PARENT_PID_ENV: "4242"}) == 4242


def test_parent_pid_from_env_rejects_missing_invalid_and_init() -> None:
    assert parent_pid_from_env({}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: ""}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: "not-a-number"}) is None
    # PID 1 (init/launchd) and non-positive PIDs are not watchable parents.
    assert parent_pid_from_env({PARENT_PID_ENV: "1"}) is None
    assert parent_pid_from_env({PARENT_PID_ENV: "0"}) is None


def test_process_alive_reports_self_and_missing() -> None:
    assert process_alive(os.getpid()) is True
    # PID 2**31 - 1 is effectively guaranteed not to exist.
    assert process_alive(2**31 - 1) is False


def test_start_parent_death_watcher_no_env_returns_none() -> None:
    assert start_parent_death_watcher(env={}) is None


def test_start_parent_death_watcher_terminates_when_parent_gone() -> None:
    # Fork a child that exits immediately, then watch it as the "parent".
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - runs only in the forked child
        os._exit(0)

    terminated = threading.Event()
    original = watch_module._terminate_self
    watch_module._terminate_self = terminated.set
    try:
        os.waitpid(child_pid, 0)  # reap so the PID is truly gone
        thread = start_parent_death_watcher(
            poll_interval_seconds=0.01,
            env={PARENT_PID_ENV: str(child_pid)},
        )
        assert thread is not None
        assert terminated.wait(timeout=2.0), "watcher did not react to dead parent"
    finally:
        watch_module._terminate_self = original
