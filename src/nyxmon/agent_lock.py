"""Single-agent lock on a monitoring database.

Exactly one monitoring agent (Python or the Rust port in ``agent-rs/``) may
run against a database. Both take a non-blocking exclusive ``flock`` on
``<db path>.agent.lock`` before touching the database and hold it until the
process exits (agent-rs/PLAN.md section 3.6). The lock is a separate file:
``flock`` on the database itself would interfere with SQLite's own locks.

Standard library only, so the Rust tests can load this module with a bare
``python3`` to prove both implementations exclude each other.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK_SUFFIX = ".agent.lock"


class AgentLockHeld(RuntimeError):
    """Another process holds the agent lock of the database."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"another nyxmon agent holds {path}; refusing to start")
        self.path = path


def agent_lock_path(db_path: str | os.PathLike[str]) -> Path:
    """Return ``<db path>.agent.lock``, derived from the path as given."""
    return Path(f"{os.fspath(db_path)}{LOCK_SUFFIX}")


def acquire_agent_lock(db_path: str | os.PathLike[str]) -> int:
    """Take the agent lock of ``db_path`` without blocking.

    Args:
        db_path: The monitoring database.

    Returns:
        The descriptor holding the lock. Keep it open for the lifetime of the
        process; closing it releases the lock.

    Raises:
        AgentLockHeld: Another process holds the lock.
        OSError: The lock file cannot be opened or locked.
    """
    path = agent_lock_path(db_path)
    # Descriptors are non-inheritable by default (PEP 446).
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise AgentLockHeld(path) from None
    except BaseException:
        os.close(fd)
        raise
    return fd
