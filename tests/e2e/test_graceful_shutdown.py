"""Real CLI processes, signals and durable SQLite claims."""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from nyxmon.adapters.collector import AsyncCheckCollector
from nyxmon.adapters.repositories import SqliteStore
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckStatus, ResultStatus
from nyxmon.domain.commands import AddCheck


WORKER = """
import os
import threading
import sys
from pathlib import Path
import anyio
from nyxmon.adapters.runner import AsyncCheckRunner
from nyxmon.adapters.notification import LoggingNotifier
from nyxmon.domain import Result, ResultStatus
from nyxmon.entrypoints.cli import start_agent

root = Path(sys.argv[1])
async def execute(self, check, send_channel):
    root.joinpath('in-flight').touch()
    if os.environ.get('NYXMON_TEST_HUNG_THREAD') == '1':
        await anyio.to_thread.run_sync(threading.Event().wait)
    while check.check_id == 2 or not root.joinpath('finish').exists():
        await anyio.sleep(0.01)
    await send_channel.send(
        Result(check_id=check.check_id, status=ResultStatus.OK, data={})
    )
AsyncCheckRunner._run_one = execute

def notify(self, check, result):
    with root.joinpath('alerts').open('a') as out:
        out.write(str(result.data) + '\\n')
LoggingNotifier.notify_check_failed = notify
sys.argv = ['start-agent', '--db', str(root / 'checks.db'), '--interval', '1']
start_agent()
"""


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("process did not reach expected state")


@pytest.fixture
def monitor(tmp_path, request):
    store = SqliteStore(db_path=tmp_path / "checks.db")
    bus = bootstrap(store=store)
    for check_id in (1, 2):
        bus.handle(
            AddCheck(
                check=Check(
                    check_id=check_id,
                    service_id=1,
                    name=f"check-{check_id}",
                    check_type="http",
                    url="https://example.test",
                    check_interval=60,
                    data={},
                )
            )
        )
    log = (tmp_path / "worker.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(tmp_path)],
        stdout=log,
        stderr=subprocess.STDOUT,
        env={
            **os.environ,
            "NYXMON_SHUTDOWN_GRACE_SECONDS": "0.3",
            "NYXMON_CHECK_BATCH_SIZE": str(getattr(request, "param", 1))
            if getattr(request, "param", 1) != "hung"
            else "1",
            "NYXMON_SITE_MODE": "off",
            "NYXMON_TEST_HUNG_THREAD": "1"
            if getattr(request, "param", 1) == "hung"
            else "0",
        },
    )
    try:
        wait_until(lambda: (tmp_path / "in-flight").exists())
        yield process, store, tmp_path
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        log.close()


@pytest.mark.anyio
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_signal_releases_unfinished_claim_without_alert(monitor, signum):
    process, store, root = monitor
    process.send_signal(signum)
    assert process.wait(timeout=10) == 0, (root / "worker.log").read_text()
    checks = await store.checks.list_async()
    assert all(check.status == CheckStatus.IDLE for check in checks)
    assert all(check.processing_started_at == 0 for check in checks)
    assert checks[0].next_check_time > int(time.time())
    assert checks[1].next_check_time == 0  # no new batch after the signal
    assert await store.results._list_async() == []
    assert not (root / "alerts").exists()
    assert store.get_collector_incident("collector:stale_processing_lease") is None


@pytest.mark.anyio
async def test_result_finishing_during_grace_is_recorded(monitor):
    process, store, root = monitor
    process.send_signal(signal.SIGTERM)
    time.sleep(0.05)
    (root / "finish").touch()
    assert process.wait(timeout=10) == 0, (root / "worker.log").read_text()
    results = await store.results._list_async()
    assert len(results) == 1
    assert results[0].status == ResultStatus.OK
    assert (await store.checks._get_async(1)).status == CheckStatus.IDLE
    assert (await store.checks._get_async(2)).next_check_time == 0
    assert not (root / "alerts").exists()


@pytest.mark.anyio
async def test_hard_kill_still_reclaims_and_alerts(monitor):
    process, store, root = monitor
    process.kill()
    assert process.wait(timeout=5) == -signal.SIGKILL
    assert (await store.checks._get_async(1)).status == CheckStatus.PROCESSING
    # Advance only the persisted lease age, instead of waiting fifteen minutes.
    with sqlite3.connect(root / "checks.db") as db:
        db.execute(
            "UPDATE health_check SET processing_started_at = ? WHERE id = 1",
            (int(time.time()) - 901,),
        )
        db.execute("UPDATE health_check SET disabled = 1 WHERE id = 2")
    notifier = MagicMock()
    collector = AsyncCheckCollector()
    bootstrap(store=store, collector=collector, notifier=notifier)
    await collector._collect_once()
    results = await store.results._list_async()
    assert len(results) == 1
    assert results[0].data["error_type"] == "stale_processing_lease"
    assert (await store.checks._get_async(1)).status == CheckStatus.IDLE
    notifier.notify_check_failed.assert_called_once()
    assert notifier.notify_check_failed.call_args.args[1].data["error_type"] == (
        "stale_processing_lease_batch"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("monitor", [2], indirect=True)
async def test_finished_check_persists_while_another_check_stays_in_flight(monitor):
    process, store, root = monitor
    process.send_signal(signal.SIGTERM)
    time.sleep(0.05)
    (root / "finish").touch()
    assert process.wait(timeout=10) == 0, (root / "worker.log").read_text()
    results = await store.results._list_async()
    assert [(result.check_id, result.status) for result in results] == [
        (1, ResultStatus.OK)
    ]
    assert all(
        check.status == CheckStatus.IDLE for check in await store.checks.list_async()
    )
    assert not (root / "alerts").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("monitor", ["hung"], indirect=True)
async def test_uninterruptible_executor_does_not_hold_process_exit(monitor):
    process, store, root = monitor
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=5) == 0, (root / "worker.log").read_text()
    assert time.monotonic() - started < 3
    assert (await store.checks._get_async(1)).status == CheckStatus.IDLE
    assert await store.results._list_async() == []
    assert not (root / "alerts").exists()
