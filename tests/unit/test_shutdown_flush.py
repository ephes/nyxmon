"""Results that finish during the shutdown grace period are recorded."""

import threading
import time
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from anyio.from_thread import BlockingPortalProvider

from nyxmon.adapters.collector import AsyncCheckCollector
from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.adapters.runner import AsyncCheckRunner
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckStatus, Result, ResultStatus
from nyxmon.domain.commands import IntakeStop


def check(check_id):
    return Check(
        check_id=check_id,
        service_id=1,
        name=str(check_id),
        check_type="http",
        url="https://example.test",
        data={},
        check_interval=60,
    )


def ok(check_id):
    return Result(check_id=check_id, status=ResultStatus.OK, data={})


@pytest.mark.anyio
async def test_intake_stop_hands_over_finished_results_and_cancels_the_rest(
    monkeypatch,
):
    runner = AsyncCheckRunner(MagicMock())
    monkeypatch.setattr(runner.executor_registry, "aclose_all", AsyncMock())
    stop = IntakeStop()
    first_receiving = anyio.Event()
    second_sent = anyio.Event()
    unfinished_cancelled = anyio.Event()
    received = []

    async def execute(check, send_channel):
        if check.check_id == 1:
            await send_channel.send(ok(1))
        elif check.check_id == 2:
            await first_receiving.wait()
            await send_channel.send(ok(2))
            second_sent.set()
        else:
            try:
                await anyio.sleep_forever()
            finally:
                unfinished_cancelled.set()

    async def receive(result):
        received.append(result.check_id)
        if result.check_id == 1:
            # A slow result handler (an alert being sent) while check 2 finishes.
            first_receiving.set()
            await second_sent.wait()
            stop.requested.set()
            await anyio.sleep(0.1)

    monkeypatch.setattr(runner, "_run_one", execute)
    with anyio.fail_after(5):
        await runner._async_run_batch([check(1), check(2), check(3)], receive, stop)
    assert received == [1, 2]
    assert unfinished_cancelled.is_set()
    assert stop.drained.is_set()


@pytest.mark.anyio
async def test_intake_stop_reports_drained_while_a_check_cannot_be_cancelled(
    monkeypatch,
):
    runner = AsyncCheckRunner(MagicMock())
    monkeypatch.setattr(runner.executor_registry, "aclose_all", AsyncMock())
    stop = IntakeStop()
    release = threading.Event()

    async def execute(check, send_channel):
        # An executor thread that ignores cancellation.
        await anyio.to_thread.run_sync(release.wait)
        await send_channel.send(ok(check.check_id))

    async def receive(result):
        raise AssertionError("a result after the intake stop is not handed over")

    monkeypatch.setattr(runner, "_run_one", execute)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(runner._async_run_batch, [check(1)], receive, stop)
        stop.requested.set()
        with anyio.fail_after(5):
            while not stop.drained.is_set():
                await anyio.sleep(0.01)
        release.set()


def test_result_queued_behind_slow_alert_is_recorded_at_shutdown(monkeypatch):
    """A result buffered behind a slow alert must not be lost when grace ends."""
    monkeypatch.setenv("NYXMON_SHUTDOWN_GRACE_SECONDS", "0.2")
    monkeypatch.setenv("NYXMON_CHECK_BATCH_SIZE", "3")
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_SITE_MODE", "off")
    store = InMemoryStore()
    for check_id in (1, 2, 3):
        store.checks.add(check(check_id))
    alert_started = threading.Event()

    def slow_alert(check, result):
        alert_started.set()
        time.sleep(0.6)  # longer than the grace period
        return True

    notifier = MagicMock()
    notifier.notify_check_failed.side_effect = slow_alert
    provider = BlockingPortalProvider()
    runner = AsyncCheckRunner(provider)
    collector = AsyncCheckCollector(interval=3600)
    bootstrap(
        store=store,
        collector=collector,
        runner=runner,
        notifier=notifier,
        portal_provider=provider,
    )
    collector.set_portal_provider(provider)
    stopping = threading.Event()

    async def execute(check, send_channel):
        if check.check_id == 1:
            await send_channel.send(
                Result(check_id=1, status=ResultStatus.ERROR, data={})
            )
        elif check.check_id == 2:
            # Finishes within the grace period, while check 1 is alerting.
            while not stopping.is_set():
                await anyio.sleep(0.01)
            await anyio.sleep(0.05)
            await send_channel.send(ok(2))
        else:
            await anyio.sleep_forever()

    monkeypatch.setattr(runner, "_run_one", execute)
    collector.start()
    try:
        assert alert_started.wait(timeout=5)
        stopping.set()
        collector.stop()
    finally:
        collector.stop()

    results = {result.check_id: result.status for result in store.results.list()}
    assert results == {1: ResultStatus.ERROR, 2: ResultStatus.OK}
    notifier.notify_check_failed.assert_called_once()
    checks = {check.check_id: check for check in store.checks.list()}
    assert all(check.status == CheckStatus.IDLE for check in checks.values())
    assert all(check.processing_started_at == 0 for check in checks.values())
