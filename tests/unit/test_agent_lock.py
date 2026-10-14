"""One monitoring agent per database (agent-rs/PLAN.md section 3.6).

The Rust agent takes the same ``flock``; its tests run this module from a
separate ``python3`` process to prove both implementations exclude each other
(``agent-rs/crates/nyxmon-store/tests/agent_lock.rs``).
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from nyxmon import agent_lock
from nyxmon.agent_lock import AgentLockHeld, acquire_agent_lock, agent_lock_path
from nyxmon.entrypoints import cli


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "db.sqlite3"
    path.touch()
    return path


def test_lock_path_is_derived_from_the_database_path(tmp_path: Path) -> None:
    assert (
        agent_lock_path(tmp_path / "db.sqlite3") == tmp_path / "db.sqlite3.agent.lock"
    )
    assert agent_lock_path("relative.db") == Path("relative.db.agent.lock")


def test_second_lock_on_the_same_database_is_refused(db_path: Path) -> None:
    fd = acquire_agent_lock(db_path)
    try:
        with pytest.raises(AgentLockHeld) as excinfo:
            acquire_agent_lock(db_path)
        assert excinfo.value.path == agent_lock_path(db_path)
        assert str(excinfo.value) == (
            f"another nyxmon agent holds {agent_lock_path(db_path)}; refusing to start"
        )
    finally:
        os.close(fd)

    # Released with the descriptor.
    os.close(acquire_agent_lock(db_path))


def test_lock_file_is_private(db_path: Path) -> None:
    os.close(acquire_agent_lock(db_path))
    mode = stat.S_IMODE(agent_lock_path(db_path).stat().st_mode)
    assert mode & 0o077 == 0


def test_start_agent_refuses_a_locked_database(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started: list[bool] = []

    async def must_not_run(*args, **kwargs) -> None:  # pragma: no cover
        started.append(True)

    monkeypatch.setattr(cli, "run_monitoring_services", must_not_run)
    monkeypatch.setattr(sys, "argv", ["start-agent", "--db", str(db_path)])
    holder = acquire_agent_lock(db_path)
    try:
        with caplog.at_level(logging.ERROR, logger=cli.__name__):
            with pytest.raises(SystemExit) as excinfo:
                cli.start_agent()
    finally:
        os.close(holder)

    assert excinfo.value.code == 1
    assert started == []
    assert (
        f"another nyxmon agent holds {agent_lock_path(db_path)}; refusing to start"
        in caplog.messages
    )


def test_start_agent_holds_the_lock_while_running(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[bool] = []

    async def services(*args, **kwargs) -> None:
        try:
            os.close(acquire_agent_lock(db_path))
        except AgentLockHeld:
            observed.append(True)

    monkeypatch.setattr(cli, "run_monitoring_services", services)
    monkeypatch.setattr(sys, "argv", ["start-agent", "--db", str(db_path)])
    monkeypatch.setattr(cli, "_agent_lock_fd", None)
    try:
        cli.start_agent()
    finally:
        if cli._agent_lock_fd is not None:
            os.close(cli._agent_lock_fd)

    assert observed == [True]
    assert agent_lock.LOCK_SUFFIX == ".agent.lock"
