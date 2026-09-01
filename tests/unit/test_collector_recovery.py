"""Tests for recovery of expired processing leases."""

from unittest.mock import AsyncMock, MagicMock
import threading

import anyio
import pytest

from nyxmon.adapters.collector import (
    AsyncCheckCollector,
    estimated_check_runtime_seconds,
    processing_lease_seconds,
)
from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.adapters.repositories.interface import check_batch_size
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckStatus, CheckType
from nyxmon.domain.commands import AddCheckResult


def _stale_check(check_id: int) -> Check:
    return Check(
        check_id=check_id,
        service_id=1,
        name=f"stale-{check_id}",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        status=CheckStatus.IDLE,
        data={},
    )


@pytest.mark.anyio
async def test_one_stale_result_failure_does_not_block_other_recoveries(caplog) -> None:
    collector = AsyncCheckCollector()
    bus = MagicMock()
    bus.handle.side_effect = [RuntimeError("database unavailable"), None]
    bus.command_handlers = {AddCheckResult: bus.handle}
    collector.set_message_bus(bus)
    collector.set_recovery_handler(bus.handle)

    await collector._record_stale_check(_stale_check(1))
    await collector._record_stale_check(_stale_check(2))

    assert bus.handle.call_count == 2
    assert "check_id=1" in caplog.text
    second_command = bus.handle.call_args_list[1].args[0]
    assert second_command.check_result.result.data["error_type"] == (
        "stale_processing_lease"
    )


@pytest.mark.anyio
async def test_reclaim_failure_does_not_block_due_checks(caplog) -> None:
    collector = AsyncCheckCollector()
    checks = MagicMock()
    checks.reclaim_stale_checks_async = AsyncMock(
        side_effect=RuntimeError("database locked")
    )
    checks.list_async = AsyncMock(return_value=[])
    due = _stale_check(7)
    checks.list_due_checks_async = AsyncMock(return_value=[due])
    bus = MagicMock()
    bus.uow.store.checks = checks
    bus.command_handlers = {AddCheckResult: bus.handle}
    collector.set_message_bus(bus)
    collector.set_recovery_handler(bus.handle)

    await collector._collect_once()

    checks.list_due_checks_async.assert_awaited_once()
    execute_command = bus.handle.call_args.args[0]
    assert execute_command.checks == [due]
    assert "processing-lease reclaim failed" in caplog.text


def test_processing_lease_configuration_logs_and_clamps(monkeypatch, caplog) -> None:
    monkeypatch.setenv("NYXMON_PROCESSING_LEASE_SECONDS", "not-a-number")
    assert processing_lease_seconds() == 900
    assert processing_lease_seconds() == 900
    assert "is invalid" in caplog.text
    assert caplog.text.count("is invalid") == 1

    caplog.clear()
    monkeypatch.setenv("NYXMON_PROCESSING_LEASE_SECONDS", "10")
    assert processing_lease_seconds() == 30
    assert "clamping to 30" in caplog.text


def test_check_batch_size_configuration_is_bounded(monkeypatch, caplog) -> None:
    monkeypatch.setenv("NYXMON_CHECK_BATCH_SIZE", "invalid")
    assert check_batch_size() == 5
    assert check_batch_size() == 5
    assert "is invalid" in caplog.text
    assert caplog.text.count("is invalid") == 1

    monkeypatch.setenv("NYXMON_CHECK_BATCH_SIZE", "0")
    assert check_batch_size() == 1
    monkeypatch.setenv("NYXMON_CHECK_BATCH_SIZE", "1000")
    assert check_batch_size() == 100


@pytest.mark.anyio
async def test_collect_once_records_stale_and_still_executes_due_checks() -> None:
    collector = AsyncCheckCollector()
    collector.set_incident_store(InMemoryStore())
    stale = _stale_check(1)
    due = _stale_check(2)
    checks = MagicMock()
    checks.reclaim_stale_checks_async = AsyncMock(side_effect=[[stale], []])
    checks.list_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[due])
    bus = MagicMock()
    bus.uow.store.checks = checks
    bus.command_handlers = {AddCheckResult: bus.handle}
    collector.set_message_bus(bus)
    collector.set_recovery_handler(bus.handle)

    await collector._collect_once()

    assert bus.handle.call_count == 2
    stale_command = bus.handle.call_args_list[0].args[0]
    stale_data = stale_command.check_result.result.data
    assert stale_data["error_type"] == "stale_processing_lease"
    # The per-check result is persisted for history, but it can never page:
    # the batch is reported once, at collector level.
    assert "notification_immediate" not in stale_data
    assert stale_data["collector_internal"] is True
    assert stale_command.check_result.force_notification is False
    execute_command = bus.handle.call_args_list[1].args[0]
    assert execute_command.checks == [due]


@pytest.mark.anyio
async def test_disabled_reclaimed_check_does_not_emit_stale_alert() -> None:
    collector = AsyncCheckCollector()
    disabled = _stale_check(1)
    disabled.disabled = True
    enabled = _stale_check(2)
    checks = MagicMock()
    checks.reclaim_stale_checks_async = AsyncMock(side_effect=[[disabled, enabled], []])
    checks.list_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[])
    bus = MagicMock()
    bus.uow.store.checks = checks
    stale_handler = MagicMock(return_value=True)
    process_notifier = MagicMock()
    bus.command_handlers = {AddCheckResult: stale_handler}
    collector.set_message_bus(bus)
    collector.set_recovery_handler(stale_handler)
    collector.set_process_notifier(process_notifier)
    collector.set_incident_store(InMemoryStore())

    await collector._collect_once()

    stale_handler.assert_called_once()
    command = stale_handler.call_args.args[0]
    assert command.check_result.check.check_id == enabled.check_id
    # One collector-level incident for the batch, never one alert per member.
    process_notifier.assert_called_once()
    assert (
        process_notifier.call_args.args[1].data["error_type"]
        == "stale_processing_lease_batch"
    )


@pytest.mark.anyio
async def test_in_memory_claim_batch_is_bounded() -> None:
    store = InMemoryStore()
    for check_id in range(1, 8):
        check = _stale_check(check_id)
        check.next_check_time = 100 - check_id
        store.checks.add(check)

    assert [check.check_id for check in await store.checks.list_due_checks_async()] == [
        7,
        6,
        5,
        4,
        3,
    ]
    assert [check.check_id for check in await store.checks.list_due_checks_async()] == [
        2,
        1,
    ]


@pytest.mark.anyio
async def test_in_memory_stale_reclaim_is_oldest_first_and_bounded() -> None:
    store = InMemoryStore()
    for check_id in range(1, 8):
        check = _stale_check(check_id)
        check.status = CheckStatus.PROCESSING
        check.processing_started_at = 100 - check_id
        check.claim_started_at = 100 - check_id
        store.checks.add(check)

    assert [
        check.check_id for check in await store.checks.reclaim_stale_checks_async(300)
    ] == [7, 6, 5, 4, 3]
    assert [
        check.check_id for check in await store.checks.reclaim_stale_checks_async(300)
    ] == [2, 1]


@pytest.mark.anyio
async def test_collector_stale_result_never_pages_per_check(monkeypatch) -> None:
    """The 36-notification storm's mechanism: one stale lease, one alert."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    collector = AsyncCheckCollector()
    notifier = MagicMock()
    store = InMemoryStore()
    bootstrap(store=store, collector=collector, notifier=notifier)
    stale = _stale_check(1)
    store.checks.add(stale)

    assert await collector._record_stale_check(stale) is True

    assert len(store.results.list()) == 1
    notifier.notify_check_failed.assert_not_called()


@pytest.mark.anyio
async def test_recovery_handler_has_independent_uow_during_live_sqlite_batch(
    tmp_path,
) -> None:
    store = SqliteStore(tmp_path / "collector.db")
    collector = AsyncCheckCollector()
    notifier = MagicMock()
    bus = bootstrap(store=store, collector=collector, notifier=notifier)
    stale = _stale_check(1)
    await store.checks._add_async(stale)
    batch_entered = threading.Event()
    release_batch = threading.Event()
    batch_errors: list[BaseException] = []

    def hold_batch_transaction() -> None:
        try:
            with bus.uow:
                batch_entered.set()
                release_batch.wait(2)
        except BaseException as exc:
            batch_errors.append(exc)

    batch_thread = threading.Thread(target=hold_batch_transaction)
    batch_thread.start()
    try:
        with anyio.fail_after(1):
            await anyio.to_thread.run_sync(batch_entered.wait)
        assert await collector._record_stale_check(stale) is True
    finally:
        release_batch.set()
        batch_thread.join(timeout=2)

    assert not batch_thread.is_alive()
    assert batch_errors == []
    assert len(await store.results._list_async()) == 1
    notifier.notify_check_failed.assert_not_called()


@pytest.mark.anyio
async def test_in_memory_legacy_claim_gets_full_lease() -> None:
    store = InMemoryStore()
    check = _stale_check(1)
    check.status = CheckStatus.PROCESSING
    check.processing_started_at = 0
    store.checks.add(check)

    assert await store.checks.reclaim_stale_checks_async(300) == []
    assert check.status == CheckStatus.PROCESSING
    assert check.processing_started_at > 0


def test_runtime_estimate_extends_lease_for_retrying_imap() -> None:
    check = Check(
        check_id=1,
        service_id=1,
        name="slow IMAP",
        check_type=CheckType.IMAP,
        url="imap.example.test",
        data={"timeout": 10, "retries": 5, "retry_delay": 60},
    )

    assert estimated_check_runtime_seconds(check) == 810


def test_runtime_estimate_supports_explicit_and_string_budgets(caplog) -> None:
    explicit = _stale_check(1)
    explicit.data = {"max_runtime_seconds": "42.5"}
    assert estimated_check_runtime_seconds(explicit) == 43

    configured = _stale_check(2)
    configured.data = {"timeout": "5", "retries": "2", "retry_delay": "1.5"}
    assert estimated_check_runtime_seconds(configured) == 48

    warning_keys: set[tuple[int, str]] = set()
    malformed = _stale_check(3)
    malformed.data = {"timeout": "invalid"}
    assert estimated_check_runtime_seconds(malformed, warning_keys) == 40
    assert estimated_check_runtime_seconds(malformed, warning_keys) == 40
    assert caplog.text.count("check_id=3 has non-numeric timeout") == 1


def test_runtime_estimate_rejects_non_finite_values(caplog) -> None:
    check = _stale_check(4)
    check.data = {
        "max_runtime_seconds": "inf",
        "retries": 1e400,
        "retry_delay": "-inf",
        "timeout": float("nan"),
    }
    warning_keys: set[tuple[int, str]] = set()

    assert estimated_check_runtime_seconds(check, warning_keys) == 40
    assert warning_keys == {
        (4, "max_runtime_seconds"),
        (4, "retries"),
        (4, "retry_delay"),
        (4, "timeout"),
    }
    assert caplog.text.count("check_id=4 has non-numeric") == 4


def test_runtime_estimate_bounds_extreme_numeric_values(caplog) -> None:
    check = _stale_check(5)
    check.data = {
        "retries": 10**400,
        "retry_delay": 1e308,
        "timeout": 1e308,
    }
    warning_keys: set[tuple[int, str]] = set()

    estimate = estimated_check_runtime_seconds(check, warning_keys)

    assert estimate > 3600
    assert warning_keys == {(5, "retries")}
    assert "check_id=5 has non-numeric retries" in caplog.text


def test_concurrent_uow_store_forks_isolate_event_sets() -> None:
    store = InMemoryStore()
    fork = store.fork_for_concurrent_uow()

    assert fork.checks.seen is not store.checks.seen
    assert fork.results.seen is not store.results.seen
    assert fork.services.seen is not store.services.seen
    assert fork.checks.checks is store.checks.checks
    assert fork.results.results is store.results.results


@pytest.mark.anyio
async def test_collect_once_extends_lease_and_warns_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.collector.processing_lease_seconds", lambda: 30
    )
    slow = _stale_check(1)
    slow.data = {"max_runtime_seconds": 90}
    checks = MagicMock()
    checks.list_async = AsyncMock(return_value=[slow])
    checks.reclaim_stale_checks_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[])
    bus = MagicMock()
    bus.uow.store.checks = checks
    collector = AsyncCheckCollector()
    collector.set_message_bus(bus)

    await collector._collect_once()
    await collector._collect_once()

    assert checks.reclaim_stale_checks_async.await_args_list[0].args == (90,)
    assert checks.reclaim_stale_checks_async.await_args_list[1].args == (90,)
    assert caplog.text.count("using 90s") == 1


@pytest.mark.anyio
async def test_collect_once_clamps_only_derived_lease(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.collector.processing_lease_seconds", lambda: 30
    )
    monkeypatch.setattr(
        "nyxmon.adapters.collector.MAX_DERIVED_PROCESSING_LEASE_SECONDS", 120
    )
    slow = _stale_check(1)
    slow.data = {"max_runtime_seconds": 9_999}
    checks = MagicMock()
    checks.list_async = AsyncMock(return_value=[slow])
    checks.reclaim_stale_checks_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[])
    bus = MagicMock()
    bus.uow.store.checks = checks
    collector = AsyncCheckCollector()
    collector.set_message_bus(bus)

    await collector._collect_once()
    collector._runtime_budget_refresh_at = 0.0
    await collector._collect_once()

    assert checks.reclaim_stale_checks_async.await_args_list[0].args == (120,)
    assert checks.reclaim_stale_checks_async.await_args_list[1].args == (120,)
    assert caplog.text.count("derived lease ceiling 120s") == 1


@pytest.mark.anyio
async def test_collect_once_reclaims_with_cached_budget_after_estimation_error(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.collector.estimated_check_runtime_seconds",
        MagicMock(side_effect=RuntimeError("malformed check")),
    )
    monkeypatch.setattr(
        "nyxmon.adapters.collector.processing_lease_seconds", lambda: 900
    )
    checks = MagicMock()
    checks.list_async = AsyncMock(return_value=[_stale_check(1)])
    checks.reclaim_stale_checks_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[])
    bus = MagicMock()
    bus.uow.store.checks = checks
    collector = AsyncCheckCollector()
    collector.set_message_bus(bus)

    await collector._collect_once()

    checks.reclaim_stale_checks_async.assert_awaited_once_with(900)
    assert "using the last safe budget" in caplog.text


@pytest.mark.anyio
async def test_batch_deadline_bounds_wedged_thread_and_rearms_after_exit(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(
        "nyxmon.adapters.collector.processing_lease_seconds", lambda: 0.05
    )
    monkeypatch.setattr("nyxmon.adapters.collector.RESULT_HANDLING_BUDGET_SECONDS", 0)
    release = threading.Event()
    collector = AsyncCheckCollector()
    collector.set_incident_store(InMemoryStore())
    abandoned = _stale_check(1)
    checks = MagicMock()
    checks.list_async = AsyncMock(return_value=[])
    checks.reclaim_stale_checks_async = AsyncMock(return_value=[])
    checks.list_due_checks_async = AsyncMock(return_value=[abandoned])
    bus = MagicMock()
    bus.uow.store.checks = checks
    bus.handle.side_effect = lambda command: release.wait()
    stale_handler = MagicMock(return_value=False)
    process_notifier = MagicMock()
    bus.command_handlers = {AddCheckResult: stale_handler}
    collector.set_message_bus(bus)
    collector.set_recovery_handler(stale_handler)
    collector.set_process_notifier(process_notifier)

    try:
        with anyio.fail_after(2):
            await collector._collect_once()
            await collector._collect_once()
            await collector._collect_once()
        # The wedged batch is bounded and reported once, at collector level.
        assert bus.handle.call_count == 1
        assert process_notifier.call_count == 1
        _, paused_result = process_notifier.call_args.args
        assert paused_result.data["error_type"] == "collector_execution_paused"
        # No per-check row is borrowed to carry the collector alert.
        stale_handler.assert_not_called()
        assert abandoned.next_check_time == 0
        assert "new executions are paused" in caplog.text

        release.set()
        with anyio.fail_after(2):
            while (
                collector._abandoned_batch_done is not None
                and not collector._abandoned_batch_done.is_set()
            ):
                await anyio.sleep(0.01)
            await collector._collect_once()
        assert bus.handle.call_count == 2
    finally:
        release.set()


@pytest.mark.anyio
async def test_abandoned_batch_alert_never_borrows_an_unrelated_check() -> None:
    collector = AsyncCheckCollector()
    collector.set_incident_store(InMemoryStore())
    collector._abandoned_batch_done = threading.Event()
    collector._abandoned_batch_checks = [_stale_check(1)]
    unrelated = _stale_check(2)
    checks = MagicMock()
    checks.list_async = AsyncMock(return_value=[unrelated])
    checks.reclaim_stale_checks_async = AsyncMock(return_value=[])
    bus = MagicMock()
    bus.uow.store.checks = checks
    stale_handler = MagicMock(return_value=True)
    process_notifier = MagicMock()
    bus.command_handlers = {AddCheckResult: stale_handler}
    collector.set_message_bus(bus)
    collector.set_recovery_handler(stale_handler)
    collector.set_process_notifier(process_notifier)

    await collector._collect_once()

    stale_handler.assert_not_called()
    process_notifier.assert_called_once()
    process_check, process_result = process_notifier.call_args.args
    assert process_check.check_id == 0
    assert process_check.name == "Nyxmon collector (execution paused)"
    assert process_result.data["error_type"] == "collector_execution_paused"
    assert unrelated.next_check_time == 0
