"""Shutdown releases are scoped to their claims and preserve alert state."""

import copy
import time
from unittest.mock import MagicMock

import anyio
import pytest

from nyxmon.adapters.collector import shutdown_grace_seconds
from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.adapters.repositories.interface import NotificationState
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckResult, CheckStatus, Result, ResultStatus
from nyxmon.domain.commands import AddCheck, AddCheckResult


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["sqlite", "memory"])
async def test_release_claims_preserves_other_claims_and_alert_state(tmp_path, backend):
    store = (
        SqliteStore(db_path=tmp_path / "checks.db")
        if backend == "sqlite"
        else InMemoryStore()
    )
    notifier = MagicMock()
    bus = bootstrap(store=store, notifier=notifier)
    for check_id in range(1, 4):
        await anyio.to_thread.run_sync(
            bus.handle,
            AddCheck(
                check=Check(
                    check_id=check_id,
                    service_id=1,
                    name=str(check_id),
                    check_type="http",
                    url="https://example.test",
                    data={},
                    check_interval=60,
                )
            ),
        )
    claims = await store.checks.list_due_checks_async()
    state = NotificationState(failure_count=3)
    await anyio.to_thread.run_sync(store.checks.set_notification_state, 1, state)
    # A superseded owner must not release check 3; check 2 is not ours to release.
    wrong_claim = copy.deepcopy(claims[2])
    wrong_claim.claim_started_at -= 1
    assert await store.checks.release_claims_async([claims[0], wrong_claim]) == 1
    checks = {check.check_id: check for check in await store.checks.list_async()}
    assert checks[1].status == CheckStatus.IDLE
    assert checks[1].next_check_time >= int(time.time()) + 59
    assert checks[2].status == checks[3].status == CheckStatus.PROCESSING
    assert await store.checks.release_claims_async([claims[0]]) == 0
    assert (
        await anyio.to_thread.run_sync(store.checks.get_notification_state, 1) == state
    )
    # A late result remains historical, but cannot change cadence or notify.
    claims[0].schedule_next_check()
    await anyio.to_thread.run_sync(
        bus.handle,
        AddCheckResult(
            check_result=CheckResult(
                check=claims[0],
                result=Result(check_id=1, status=ResultStatus.ERROR, data={}),
            )
        ),
    )
    notifier.notify_check_failed.assert_not_called()
    assert (
        await anyio.to_thread.run_sync(store.checks.get_notification_state, 1) == state
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 5),
        ("0", 0),
        ("0.25", 0.25),
        ("30", 30),
        ("31", 5),
        ("-1", 5),
        ("nan", 5),
        ("inf", 5),
        ("invalid", 5),
    ],
)
def test_shutdown_grace_configuration(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("NYXMON_SHUTDOWN_GRACE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("NYXMON_SHUTDOWN_GRACE_SECONDS", value)
    assert shutdown_grace_seconds() == expected


def test_collector_rejects_start_without_bus():
    from nyxmon.adapters.collector import AsyncCheckCollector

    with pytest.raises(ValueError, match="Message bus"):
        AsyncCheckCollector().start()


def test_repeated_start_is_idempotent_and_idle_shutdown_is_prompt(monkeypatch):
    from nyxmon.adapters.collector import AsyncCheckCollector

    monkeypatch.setenv("NYXMON_SHUTDOWN_GRACE_SECONDS", "5")
    collector = AsyncCheckCollector(interval=3600)
    from anyio.from_thread import BlockingPortalProvider

    provider = BlockingPortalProvider()
    bootstrap(store=InMemoryStore(), collector=collector, portal_provider=provider)
    collector.set_portal_provider(provider)
    collector.start()
    thread = collector._thread
    try:
        collector.start()
        assert collector._thread is thread
        started = time.monotonic()
        collector.stop()
        assert time.monotonic() - started < 1
    finally:
        collector.stop()


def test_shutdown_during_stale_recovery_keeps_results_and_alert(monkeypatch):
    """A stop that arrives mid-recovery must not drop the reclaimed evidence."""
    import threading

    from anyio.from_thread import BlockingPortalProvider

    from nyxmon.adapters.collector import AsyncCheckCollector

    monkeypatch.setenv("NYXMON_SHUTDOWN_GRACE_SECONDS", "0")
    store = InMemoryStore()
    for check_id in (1, 2):
        store.checks.add(
            Check(
                check_id=check_id,
                service_id=1,
                name=str(check_id),
                check_type="http",
                url="https://example.test",
                data={},
                status=CheckStatus.PROCESSING,
                processing_started_at=int(time.time()) - 10_000,
            )
        )
    notifier = MagicMock()
    collector = AsyncCheckCollector(interval=3600)
    provider = BlockingPortalProvider()
    bootstrap(
        store=store, collector=collector, notifier=notifier, portal_provider=provider
    )
    collector.set_portal_provider(provider)
    original = collector._recovery_handler
    recording = threading.Event()

    def slow_recovery(command):
        # Stop arrives after reclaim committed, while the first result is open.
        recording.set()
        assert collector._stop_requested.wait(timeout=5)
        time.sleep(0.2)
        return original(command)

    collector.set_recovery_handler(slow_recovery)
    collector.start()
    try:
        assert recording.wait(timeout=5)
        collector.stop()
    finally:
        collector.stop()

    results = store.results.list()
    assert sorted(result.check_id for result in results) == [1, 2]
    assert {result.data["error_type"] for result in results} == {
        "stale_processing_lease"
    }
    notifier.notify_check_failed.assert_called_once()
    assert notifier.notify_check_failed.call_args.args[1].data["error_type"] == (
        "stale_processing_lease_batch"
    )


def test_failed_lease_release_does_not_hang_shutdown(monkeypatch, caplog):
    from anyio.from_thread import BlockingPortalProvider

    from nyxmon.adapters.collector import AsyncCheckCollector

    store = InMemoryStore()

    async def fail(checks):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store.checks, "release_claims_async", fail)
    collector = AsyncCheckCollector(interval=3600)
    provider = BlockingPortalProvider()
    bootstrap(store=store, collector=collector, portal_provider=provider)
    collector.set_portal_provider(provider)
    collector.start()
    time.sleep(0.1)
    import threading

    stopper = threading.Thread(target=collector.stop, daemon=True)
    stopper.start()
    stopper.join(timeout=2)
    assert not stopper.is_alive()
    assert "failed to release check leases on shutdown" in caplog.text
