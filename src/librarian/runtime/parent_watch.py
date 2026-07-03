"""Terminate the backend when a designated parent process dies.

Desktop launchers (e.g. the macOS app) start the API as a child process and
pass their own PID via ``LIBRARIAN_PARENT_PID``. If the launcher crashes or is
force-quit, the child would otherwise linger as an orphan holding the port.
This watcher polls the parent and shuts the backend down when it disappears.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Mapping

PARENT_PID_ENV = "LIBRARIAN_PARENT_PID"

_LOGGER = logging.getLogger("librarian.runtime.parent_watch")


def parent_pid_from_env(env: Mapping[str, str] | None = None) -> int | None:
    """Parse a valid, watchable parent PID from the environment, if configured."""
    environ = os.environ if env is None else env
    raw = environ.get(PARENT_PID_ENV)
    if not raw:
        return None
    try:
        parent_pid = int(raw)
    except ValueError:
        return None
    # PID 1 (init/launchd) is not a meaningful parent to watch, and non-positive
    # PIDs are invalid.
    if parent_pid <= 1:
        return None
    return parent_pid


def process_alive(pid: int) -> bool:
    """Return whether a process with ``pid`` currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by another user.
        return True
    except OSError:
        return False
    return True


def start_parent_death_watcher(
    *,
    poll_interval_seconds: float = 2.0,
    env: Mapping[str, str] | None = None,
) -> threading.Thread | None:
    """Start a daemon thread that shuts the process down if the parent dies.

    Returns the watcher thread, or ``None`` when no valid parent PID is
    configured (so the backend runs normally when launched standalone).
    """
    parent_pid = parent_pid_from_env(env)
    if parent_pid is None:
        return None

    def watch() -> None:
        while True:
            time.sleep(poll_interval_seconds)
            if not process_alive(parent_pid):
                _LOGGER.warning(
                    "parent process %d exited; shutting down backend", parent_pid
                )
                _terminate_self()
                return

    thread = threading.Thread(target=watch, name="librarian-parent-watch", daemon=True)
    thread.start()
    return thread


def _terminate_self() -> None:
    # Prefer a graceful SIGTERM so the server can unwind; fall back to a hard
    # exit if the signal is unavailable or ignored.
    try:
        os.kill(os.getpid(), signal.SIGTERM)
    except (OSError, ValueError, AttributeError):
        os._exit(1)
