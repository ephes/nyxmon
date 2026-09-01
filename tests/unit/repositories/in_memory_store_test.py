"""Compare-and-swap contract of the in-memory store.

The in-memory store backs the unit tests for the whole service layer, so it
must enforce the same optimistic-concurrency contract as the SQLite store:
``persist_check_result`` compares the current notification state with the
``expected_state`` its caller computed from and refuses the write on mismatch.
Without that, two forked units of work that read the same snapshot could both
"win", overwrite each other's failure samples, and each send the same alert.
"""

from __future__ import annotations

import pytest

from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.adapters.repositories.interface import (
    NotificationState,
    NotificationStateConflict,
)
from nyxmon.domain import Check, CheckStatus, CheckType, Result, ResultStatus


def _check(check_id: int = 1) -> Check:
    return Check(
        check_id=check_id,
        service_id=1,
        name="CAS owner",
        check_type=CheckType.HTTP,
        url="https://example.test/cas",
        data={},
    )


def _error(check_id: int = 1) -> Result:
    return Result(check_id=check_id, status=ResultStatus.ERROR, data={})


def test_concurrent_forked_units_of_work_conflict_on_the_same_snapshot() -> None:
    store = InMemoryStore()
    store.checks.add(_check())
    first = store.fork_for_concurrent_uow()
    second = store.fork_for_concurrent_uow()

    # Both units read the same state and compute a transition from it.
    snapshot = first.checks.get_notification_state(1)
    assert second.checks.get_notification_state(1) == snapshot

    assert first.persist_check_result(
        _check(),
        _error(),
        (snapshot, NotificationState(1, 0, 0)),
        complete_check=False,
    )

    with pytest.raises(NotificationStateConflict):
        second.persist_check_result(
            _check(),
            _error(),
            (snapshot, NotificationState(1, 1, 0)),
            complete_check=False,
        )

    # The winner's state stands; the loser's result and state are not stored.
    assert store.checks.get_notification_state(1) == NotificationState(1, 0, 0)
    assert len(store.results.list()) == 1


def test_conflict_leaves_the_check_claim_untouched() -> None:
    """Mirror of the SQLite atomic-rollback test: a conflict persists nothing."""
    store = InMemoryStore()
    stored = _check()
    stored.status = CheckStatus.PROCESSING
    stored.processing_started_at = 100
    store.checks.add(stored)
    store.checks.set_notification_state(1, NotificationState(1, 0, 0))

    completing = _check()
    completing.status = CheckStatus.IDLE
    completing.next_check_time = 999
    completing.claim_started_at = 100

    with pytest.raises(NotificationStateConflict):
        store.persist_check_result(
            completing,
            _error(),
            (NotificationState(), NotificationState(1, 1, 0)),
        )

    assert store.checks.get(1).status == CheckStatus.PROCESSING
    assert store.checks.get(1).processing_started_at == 100
    assert store.checks.get(1).next_check_time != 999
    assert store.checks.get_notification_state(1) == NotificationState(1, 0, 0)
    assert store.results.list() == []


def test_conflict_is_detected_on_reminder_timestamps_alone() -> None:
    """Every field of the state record takes part in the comparison."""
    store = InMemoryStore()
    store.checks.add(_check())
    store.checks.set_notification_state(1, NotificationState(3, 3, 0, 5_000, 4_000))

    stale_expectation = NotificationState(3, 3, 0, 1_000, 4_000)
    with pytest.raises(NotificationStateConflict):
        store.persist_check_result(
            _check(),
            _error(),
            (stale_expectation, NotificationState(4, 4, 0, 9_000, 4_000)),
            complete_check=False,
        )

    assert store.checks.get_notification_state(1) == NotificationState(
        3, 3, 0, 5_000, 4_000
    )
    assert store.results.list() == []


def test_matching_expectation_persists_result_and_state() -> None:
    store = InMemoryStore()
    store.checks.add(_check())

    assert store.persist_check_result(
        _check(),
        _error(),
        (NotificationState(), NotificationState(1, 0, 0)),
        complete_check=False,
    )

    assert store.checks.get_notification_state(1) == NotificationState(1, 0, 0)
    assert len(store.results.list()) == 1
