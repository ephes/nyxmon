"""Persistence for the site-connectivity hold and the per-check delivery intent.

Covers the three new ``check_notification_state`` columns (``held_since``,
``attempt_seq``, ``attempt_at``), the repository operations the observer and the
handler build on, and the two atomicity contracts that make the feature safe
across a crash: the hold marker of the conflict-exhaustion fallback commits with
the completion it belongs to, and a granted collector-incident claim records its
delivery intent inside the claim transaction.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import aiosqlite
import pytest
from anyio.from_thread import BlockingPortalProvider

from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.adapters.repositories.interface import (
    HeldCheck,
    NotificationState,
    NotificationStateConflict,
)
from nyxmon.adapters.repositories.sqlite_repo import SqliteCheckRepository
from nyxmon.domain import Check, CheckStatus, CheckType, Result, ResultStatus


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = Path(handle.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _check(
    check_id: int,
    *,
    status: str = CheckStatus.IDLE,
    next_check_time: int = 0,
    processing_started_at: int = 0,
    disabled: bool = False,
    data: dict | None = None,
) -> Check:
    return Check(
        check_id=check_id,
        service_id=1,
        name=f"check-{check_id}",
        check_type=CheckType.HTTP,
        url=f"https://example.test/{check_id}",
        check_interval=300,
        next_check_time=next_check_time,
        processing_started_at=processing_started_at,
        status=status,
        disabled=disabled,
        data=data if data is not None else {},
    )


def _error(check_id: int) -> Result:
    return Result(check_id=check_id, status=ResultStatus.ERROR, data={})


def _state_row(db_path: Path, check_id: int) -> tuple | None:
    """Read a state row through an independent connection.

    Reopening the file is what makes the atomicity assertions meaningful: a
    value only visible on the writing connection would prove nothing about what
    survives a crash.
    """
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT failure_count, last_attempt_count, last_immediate_at, "
            "last_notified_at, first_failure_at, held_since, attempt_seq, "
            "attempt_at FROM check_notification_state WHERE check_id = ?",
            (check_id,),
        ).fetchone()


# --------------------------------------------------------------------------
# NotificationState record
# --------------------------------------------------------------------------


def test_state_has_eight_fields_in_column_order() -> None:
    state = NotificationState(1, 2, 3, 4, 5, 6, 7, 8)

    assert state.as_row() == (1, 2, 3, 4, 5, 6, 7, 8)
    assert state.held_since == 6
    assert state.attempt_seq == 7
    assert state.attempt_at == 8


def test_from_row_pads_a_pre_upgrade_row() -> None:
    """A 0012-era row has five columns; the new ones default to zero."""
    assert NotificationState.from_row((3, 3, 0, 900, 800)) == NotificationState(
        3, 3, 0, 900, 800, 0, 0, 0
    )
    assert NotificationState.from_row(None) == NotificationState()


def test_resets_keep_the_attempt_sequence() -> None:
    """``attempt_seq`` is a fence, not incident state: it never goes backwards."""
    state = NotificationState(
        4, 4, 111, 900, 800, held_since=700, attempt_seq=9, attempt_at=950
    )

    assert state.cleared() == NotificationState(attempt_seq=9)
    # A maintenance-suppressed sample breaks the streak only. The hold marker
    # and the unacknowledged delivery intent are not its business.
    assert state.with_streak_reset() == NotificationState(
        last_immediate_at=111, held_since=700, attempt_seq=9, attempt_at=950
    )
    # Everything else really is reset.
    assert state.cleared().held_since == 0
    assert state.cleared().attempt_at == 0
    assert state.with_streak_reset().failure_count == 0
    assert state.with_streak_reset().last_notified_at == 0
    assert state.with_streak_reset().first_failure_at == 0


# --------------------------------------------------------------------------
# Round-trip and compare-and-swap through persist_check_result
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_all_eight_fields_round_trip_through_persist(temp_db) -> None:
    store = SqliteStore(temp_db)
    check = _check(1)
    await store.checks._add_async(check)
    full = NotificationState(2, 2, 500, 900, 800, 700, 5, 950)

    assert await store._persist_check_result_async(
        check, _error(1), (NotificationState(), full)
    )

    assert await store.checks._get_notification_state_async(1) == full
    assert _state_row(temp_db, 1) == (2, 2, 500, 900, 800, 700, 5, 950)


@pytest.mark.anyio
async def test_stale_expectation_on_a_new_field_still_conflicts(temp_db) -> None:
    """The compare-and-swap covers the new columns too.

    Two workers must not be able to each claim the same delivery attempt just
    because they agree on the streak fields.
    """
    store = SqliteStore(temp_db)
    check = _check(1)
    await store.checks._add_async(check)
    await store.checks._set_notification_state_async(
        1, NotificationState(3, 3, 0, 900, 800, 700, 4, 950)
    )
    stale = NotificationState(3, 3, 0, 900, 800, 700, 3, 0)

    with pytest.raises(NotificationStateConflict):
        await store._persist_check_result_async(
            check,
            _error(1),
            (stale, NotificationState(4, 4, 0, 900, 800, 700, 4, 1_000)),
            complete_check=False,
        )

    assert await store.checks._get_notification_state_async(1) == NotificationState(
        3, 3, 0, 900, 800, 700, 4, 950
    )
    assert await store.results._list_async() == []


def test_in_memory_persist_round_trips_the_new_fields() -> None:
    store = InMemoryStore()
    check = _check(1)
    store.checks.add(check)
    full = NotificationState(2, 2, 500, 900, 800, 700, 5, 950)

    assert store.persist_check_result(check, _error(1), (NotificationState(), full))

    assert store.checks.get_notification_state(1) == full


# --------------------------------------------------------------------------
# acknowledge_notification_attempt
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_acknowledgement_is_fenced_by_the_attempt_sequence(temp_db) -> None:
    store = SqliteStore(temp_db)
    await store.checks._add_async(_check(1))
    await store.checks._set_notification_state_async(
        1, NotificationState(2, 2, 0, 900, 800, 0, 7, 950)
    )

    # A stale sequence number (an older, slower send) changes nothing.
    assert await store.checks._acknowledge_notification_attempt_async(1, 6) is False
    assert (await store.checks._get_notification_state_async(1)).attempt_at == 950

    assert await store.checks._acknowledge_notification_attempt_async(1, 7) is True
    state = await store.checks._get_notification_state_async(1)
    assert state.attempt_at == 0
    assert state.attempt_seq == 7

    # A second acknowledgement of the same attempt is a no-op, not a rewrite.
    assert await store.checks._acknowledge_notification_attempt_async(1, 7) is False


@pytest.mark.anyio
async def test_acknowledgement_after_an_ok_cleared_the_row_changes_nothing(
    temp_db,
) -> None:
    """Failure, recovery, and a late acknowledgement of the failed alert.

    ``cleared()`` keeps ``attempt_seq``, so the sequence still matches - but
    ``attempt_at`` is already 0 and there is nothing to acknowledge.
    """
    store = SqliteStore(temp_db)
    await store.checks._add_async(_check(1))
    await store.checks._set_notification_state_async(
        1, NotificationState(2, 2, 0, 900, 800, 0, 7, 950)
    )
    recovered = NotificationState(2, 2, 0, 900, 800, 0, 7, 950).cleared()
    await store.checks._set_notification_state_async(1, recovered)

    assert await store.checks._acknowledge_notification_attempt_async(1, 7) is False
    assert await store.checks._get_notification_state_async(1) == recovered


def test_in_memory_acknowledgement_is_fenced_the_same_way() -> None:
    store = InMemoryStore()
    store.checks.add(_check(1))
    store.checks.set_notification_state(
        1, NotificationState(2, 2, 0, 900, 800, 0, 7, 950)
    )

    assert store.checks.acknowledge_notification_attempt(1, 6) is False
    assert store.checks.acknowledge_notification_attempt(1, 7) is True
    assert store.checks.get_notification_state(1).attempt_at == 0
    assert store.checks.acknowledge_notification_attempt(1, 7) is False

    # Nothing persisted for an unknown check.
    assert store.checks.acknowledge_notification_attempt(99, 1) is False


def test_sqlite_acknowledgement_is_reachable_through_the_sync_port(temp_db) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        with portal_provider as portal:
            portal.call(store.checks._add_async, _check(1))
            portal.call(
                store.checks._set_notification_state_async,
                1,
                NotificationState(1, 1, 0, 900, 800, 0, 3, 950),
            )

        assert store.checks.acknowledge_notification_attempt(1, 3) is True
        assert store.checks.acknowledge_notification_attempt(1, 3) is False


# --------------------------------------------------------------------------
# list_held_checks / count_held_checks
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_held_checks_joins_state_and_check(temp_db) -> None:
    store = SqliteStore(temp_db)
    dependency = {"site_dependency": "internet"}
    await store.checks._add_async(_check(1, next_check_time=5_000, data=dependency))
    await store.checks._add_async(_check(2, next_check_time=6_000))
    await store.checks._add_async(_check(3, disabled=True, next_check_time=7_000))
    await store.checks._set_notification_state_async(
        1, NotificationState(2, 0, 0, 0, 800, 900, 0, 0)
    )
    # A failing but unheld check is not part of the recheck obligation.
    await store.checks._set_notification_state_async(
        2, NotificationState(2, 0, 0, 0, 800, 0, 0, 0)
    )
    await store.checks._set_notification_state_async(
        3, NotificationState(1, 0, 0, 0, 800, 950, 0, 0)
    )

    held = await store.checks.list_held_checks_async()

    assert held == [
        HeldCheck(
            check_id=1,
            data=dependency,
            status=CheckStatus.IDLE,
            disabled=False,
            next_check_time=5_000,
            held_since=900,
        ),
        HeldCheck(
            check_id=3,
            data={},
            status=CheckStatus.IDLE,
            disabled=True,
            next_check_time=7_000,
            held_since=950,
        ),
    ]
    assert await store.checks.count_held_checks_async() == 2


@pytest.mark.anyio
async def test_in_memory_held_checks_match_sqlite() -> None:
    store = InMemoryStore()
    dependency = {"site_dependency": {"requires": ["dns"]}}
    store.checks.add(_check(1, next_check_time=5_000, data=dependency))
    store.checks.add(_check(2, next_check_time=6_000))
    store.checks.set_notification_state(
        1, NotificationState(2, 0, 0, 0, 800, 900, 0, 0)
    )
    # A failing but unheld check owes no recheck.
    store.checks.set_notification_state(2, NotificationState(2, 0, 0, 0, 800, 0, 0, 0))

    held = await store.checks.list_held_checks_async()

    assert held == [
        HeldCheck(
            check_id=1,
            data=dependency,
            status=CheckStatus.IDLE,
            disabled=False,
            next_check_time=5_000,
            held_since=900,
        )
    ]
    assert await store.checks.count_held_checks_async() == 1


def test_sqlite_count_held_checks_is_reachable_through_the_sync_port(temp_db) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        with portal_provider as portal:
            portal.call(store.checks._add_async, _check(1))
            portal.call(
                store.checks._set_notification_state_async,
                1,
                NotificationState(held_since=900),
            )

        assert store.checks.count_held_checks() == 1


# --------------------------------------------------------------------------
# reschedule_checks
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reschedule_touches_only_idle_enabled_and_later_checks(temp_db) -> None:
    store = SqliteStore(temp_db)
    await store.checks._add_async(_check(1, next_check_time=9_000))
    await store.checks._add_async(_check(2, next_check_time=9_000, disabled=True))
    await store.checks._add_async(
        _check(
            3,
            next_check_time=9_000,
            status=CheckStatus.PROCESSING,
            processing_started_at=4_000,
        )
    )
    # Already due earlier: a recheck must never delay a check.
    await store.checks._add_async(_check(4, next_check_time=4_000))

    assert await store.checks.reschedule_checks_async([1, 2, 3, 4], run_at=5_000) == 1

    scheduled = {
        check.check_id: check.next_check_time
        for check in await store.checks.list_async()
    }
    assert scheduled == {1: 5_000, 2: 9_000, 3: 9_000, 4: 4_000}


@pytest.mark.anyio
async def test_reschedule_with_no_ids_is_a_no_op(temp_db) -> None:
    store = SqliteStore(temp_db)
    await store.checks._add_async(_check(1, next_check_time=9_000))

    assert await store.checks.reschedule_checks_async([], run_at=5_000) == 0
    assert (await store.checks.list_async())[0].next_check_time == 9_000


@pytest.mark.anyio
async def test_in_memory_reschedule_matches_sqlite() -> None:
    store = InMemoryStore()
    store.checks.add(_check(1, next_check_time=9_000))
    store.checks.add(_check(2, next_check_time=9_000, disabled=True))
    store.checks.add(
        _check(
            3,
            next_check_time=9_000,
            status=CheckStatus.PROCESSING,
            processing_started_at=4_000,
        )
    )
    store.checks.add(_check(4, next_check_time=4_000))

    assert (
        await store.checks.reschedule_checks_async([1, 2, 3, 4, 99], run_at=5_000) == 1
    )
    assert await store.checks.reschedule_checks_async([], run_at=1) == 0

    scheduled = {check.check_id: check.next_check_time for check in store.checks.list()}
    assert scheduled == {1: 5_000, 2: 9_000, 3: 9_000, 4: 4_000}


# --------------------------------------------------------------------------
# count_processing_claims_before
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_counts_only_processing_claims_older_than_the_epoch(temp_db) -> None:
    store = SqliteStore(temp_db)
    await store.checks._add_async(
        _check(1, status=CheckStatus.PROCESSING, processing_started_at=4_000)
    )
    # Claimed exactly at the release: measured after it, so not a pre-release claim.
    await store.checks._add_async(
        _check(2, status=CheckStatus.PROCESSING, processing_started_at=5_000)
    )
    await store.checks._add_async(
        _check(3, status=CheckStatus.PROCESSING, processing_started_at=6_000)
    )
    # Idle rows never count, whatever their stale claim timestamp says.
    await store.checks._add_async(_check(4, processing_started_at=1_000))
    # A malformed claim without a timestamp is not a datable pre-release claim.
    await store.checks._add_async(
        _check(5, status=CheckStatus.PROCESSING, processing_started_at=0)
    )

    assert await store.checks.count_processing_claims_before_async(5_000) == 1
    assert await store.checks.count_processing_claims_before_async(6_001) == 3
    assert await store.checks.count_processing_claims_before_async(0) == 0


@pytest.mark.anyio
async def test_in_memory_processing_claim_count_matches_sqlite() -> None:
    store = InMemoryStore()
    store.checks.add(
        _check(1, status=CheckStatus.PROCESSING, processing_started_at=4_000)
    )
    store.checks.add(
        _check(2, status=CheckStatus.PROCESSING, processing_started_at=5_000)
    )
    store.checks.add(_check(3, processing_started_at=1_000))
    store.checks.add(_check(4, status=CheckStatus.PROCESSING, processing_started_at=0))

    assert await store.checks.count_processing_claims_before_async(5_000) == 1
    assert await store.checks.count_processing_claims_before_async(6_000) == 2


# --------------------------------------------------------------------------
# hold_marker: the conflict-exhaustion fallback
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_hold_marker_lands_atomically_with_the_completion(temp_db) -> None:
    """Both writes or neither, observed through a reopened connection.

    The observer counts in-flight claims first and reads the held set second.
    If the completion could commit without the hold, it would see an idle,
    unheld row for a claim that still owes a recheck.
    """
    store = SqliteStore(temp_db)
    check = _check(
        1, status=CheckStatus.PROCESSING, processing_started_at=4_000, next_check_time=0
    )
    await store.checks._add_async(check)
    completed = _check(1, next_check_time=9_000)
    completed.claim_started_at = 4_000

    assert await store._persist_check_result_async(
        completed, _error(1), None, True, 4_500
    )

    with sqlite3.connect(temp_db) as connection:
        status, next_check_time, processing_started_at = connection.execute(
            "SELECT status, next_check_time, processing_started_at "
            "FROM health_check WHERE id = 1"
        ).fetchone()
    assert (status, next_check_time, processing_started_at) == (
        CheckStatus.IDLE,
        9_000,
        0,
    )
    assert _state_row(temp_db, 1) == (0, 0, 0, 0, 0, 4_500, 0, 0)


@pytest.mark.anyio
async def test_hold_marker_never_overwrites_a_running_hold(temp_db) -> None:
    """The budget is bounded from the *first* held sample, not the latest one."""
    store = SqliteStore(temp_db)
    check = _check(1)
    await store.checks._add_async(check)
    await store.checks._set_notification_state_async(
        1, NotificationState(3, 0, 0, 0, 800, 900, 2, 0)
    )

    assert await store._persist_check_result_async(check, _error(1), None, True, 9_999)

    assert _state_row(temp_db, 1) == (3, 0, 0, 0, 800, 900, 2, 0)


@pytest.mark.anyio
async def test_hold_marker_is_stamped_on_an_existing_unheld_row(temp_db) -> None:
    store = SqliteStore(temp_db)
    check = _check(1)
    await store.checks._add_async(check)
    await store.checks._set_notification_state_async(
        1, NotificationState(3, 0, 0, 0, 800, 0, 2, 0)
    )

    assert await store._persist_check_result_async(check, _error(1), None, True, 4_500)

    assert _state_row(temp_db, 1) == (3, 0, 0, 0, 800, 4_500, 2, 0)


@pytest.mark.anyio
async def test_a_stale_completion_marks_nothing(temp_db) -> None:
    """A result whose claim was superseded is stored but owes no recheck.

    Whoever holds the current claim will decide the hold for that execution.
    """
    store = SqliteStore(temp_db)
    await store.checks._add_async(
        _check(1, status=CheckStatus.PROCESSING, processing_started_at=8_000)
    )
    late = _check(1, next_check_time=9_000)
    late.claim_started_at = 4_000

    assert (
        await store._persist_check_result_async(late, _error(1), None, True, 4_500)
        is False
    )

    assert len(await store.results._list_async()) == 1
    assert _state_row(temp_db, 1) is None


@pytest.mark.anyio
async def test_a_deleted_check_marks_nothing(temp_db) -> None:
    store = SqliteStore(temp_db)
    ghost = _check(1)
    ghost.claim_started_at = 4_000

    assert (
        await store._persist_check_result_async(ghost, _error(1), None, True, 4_500)
        is False
    )

    assert await store.results._list_async() == []
    assert _state_row(temp_db, 1) is None


@pytest.mark.anyio
async def test_hold_marker_is_ignored_when_a_transition_is_given(temp_db) -> None:
    """A successful compare-and-swap owns ``held_since`` itself."""
    store = SqliteStore(temp_db)
    check = _check(1)
    await store.checks._add_async(check)

    assert await store._persist_check_result_async(
        check,
        _error(1),
        (NotificationState(), NotificationState(1, 0, 0, 0, 800, 0, 0, 0)),
        True,
        4_500,
    )

    assert _state_row(temp_db, 1) == (1, 0, 0, 0, 800, 0, 0, 0)


def test_in_memory_hold_marker_matches_sqlite() -> None:
    store = InMemoryStore()
    store.checks.add(_check(1))

    assert store.persist_check_result(_check(1), _error(1), None, hold_marker=4_500)
    assert store.checks.get_notification_state(1).held_since == 4_500

    # A second held sample does not re-arm the budget.
    assert store.persist_check_result(_check(1), _error(1), None, hold_marker=9_999)
    assert store.checks.get_notification_state(1).held_since == 4_500


def test_in_memory_stale_completion_marks_no_hold() -> None:
    store = InMemoryStore()
    store.checks.add(
        _check(1, status=CheckStatus.PROCESSING, processing_started_at=8_000)
    )
    late = _check(1)
    late.claim_started_at = 4_000

    assert store.persist_check_result(late, _error(1), None, hold_marker=4_500) is False
    assert store.checks.get_notification_state(1).held_since == 0


# --------------------------------------------------------------------------
# Collector incidents: silent open and delivery intent
# --------------------------------------------------------------------------


OPENED_AT = 1_760_000_000  # a wall-clock epoch, as every call site passes


def _exercise_open(store) -> None:
    opened = store.open_collector_incident(
        "site:connectivity", now=OPENED_AT, payload={"paths": ["ipv4"]}
    )

    assert opened.opened_at == OPENED_AT
    assert opened.last_alert_at == 0
    assert opened.alert_count == 0
    assert opened.payload == {"paths": ["ipv4"]}

    # Re-opening only replaces the payload; the cadence stays untouched.
    refreshed = store.open_collector_incident(
        "site:connectivity", now=OPENED_AT + 9_999, payload={"paths": ["ipv4", "dns"]}
    )
    assert refreshed.opened_at == OPENED_AT
    assert refreshed.last_alert_at == 0
    assert refreshed.alert_count == 0
    assert refreshed.payload == {"paths": ["ipv4", "dns"]}

    # The silently opened row alerts on the first claim after the notify-after
    # threshold: the existing formula is ``now - last_alert_at >= reminder`` and
    # ``last_alert_at`` is 0, which any wall-clock ``now`` exceeds. The caller
    # therefore controls the delay by when it claims, not by the cadence fields.
    claimed = store.claim_collector_incident_alert(
        "site:connectivity", now=OPENED_AT + 900, reminder_seconds=21_600
    )
    assert claimed.should_notify is True
    assert claimed.is_new is False
    assert claimed.incident.opened_at == OPENED_AT
    assert claimed.incident.alert_count == 1
    assert claimed.incident.payload == {
        "paths": ["ipv4", "dns"],
        "delivery_pending": True,
        "delivery_attempt": 1,
    }

    # And the reminder cadence starts from that first alert, not from the open.
    quiet = store.claim_collector_incident_alert(
        "site:connectivity", now=OPENED_AT + 1_000, reminder_seconds=21_600
    )
    assert quiet.should_notify is False


def test_in_memory_open_collector_incident_is_silent() -> None:
    _exercise_open(InMemoryStore())


def test_sqlite_open_collector_incident_is_silent(temp_db) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        _exercise_open(store)


def _exercise_delivery_markers(store) -> None:
    granted = store.claim_collector_incident_alert(
        "stale_batch", now=1_000, reminder_seconds=3_600, payload={"check_ids": [1]}
    )
    assert granted.should_notify is True
    assert granted.incident.payload == {
        "check_ids": [1],
        "delivery_pending": True,
        "delivery_attempt": 1,
    }

    # A non-granting refresh with a freshly built payload must not erase the
    # pending intent: a crash after this write would otherwise lose the alert.
    refreshed = store.claim_collector_incident_alert(
        "stale_batch", now=1_100, reminder_seconds=3_600, payload={"check_ids": [1, 2]}
    )
    assert refreshed.should_notify is False
    assert refreshed.incident.payload == {
        "check_ids": [1, 2],
        "delivery_pending": True,
        "delivery_attempt": 1,
    }

    # Opening the same incident again preserves them too.
    reopened = store.open_collector_incident(
        "stale_batch", now=1_200, payload={"check_ids": [3]}
    )
    assert reopened.payload == {
        "check_ids": [3],
        "delivery_pending": True,
        "delivery_attempt": 1,
    }

    # Only the acknowledgement path may drop the markers.
    acknowledged = store.set_collector_incident_payload(
        "stale_batch", {"check_ids": [3]}
    )
    assert acknowledged is not None
    assert acknowledged.payload == {"check_ids": [3]}
    assert store.get_collector_incident("stale_batch").payload == {"check_ids": [3]}

    # The next granted reminder records a fresh intent with the new attempt.
    reminder = store.claim_collector_incident_alert(
        "stale_batch", now=4_700, reminder_seconds=3_600
    )
    assert reminder.should_notify is True
    assert reminder.incident.payload == {
        "check_ids": [3],
        "delivery_pending": True,
        "delivery_attempt": 2,
    }


def test_in_memory_claim_records_delivery_intent() -> None:
    _exercise_delivery_markers(InMemoryStore())


def test_sqlite_claim_records_delivery_intent(temp_db) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        _exercise_delivery_markers(store)


def test_delivery_intent_is_committed_with_the_claim(temp_db) -> None:
    """The intent survives a crash right after the claim, before any send."""
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        store.claim_collector_incident_alert(
            "stale_batch", now=1_000, reminder_seconds=3_600, payload={"check_ids": [1]}
        )

    with sqlite3.connect(temp_db) as connection:
        last_alert_at, alert_count, payload = connection.execute(
            "SELECT last_alert_at, alert_count, payload "
            "FROM collector_incident WHERE incident_key = 'stale_batch'"
        ).fetchone()

    assert (last_alert_at, alert_count) == (1_000, 1)
    assert json.loads(payload) == {
        "check_ids": [1],
        "delivery_pending": True,
        "delivery_attempt": 1,
    }


# --------------------------------------------------------------------------
# Worker-side schema upgrade
# --------------------------------------------------------------------------


async def _create_pre_upgrade_schema(db_path: Path) -> None:
    """Recreate the 0012-era table: five state columns, nothing else."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE health_check (
                id               INTEGER PRIMARY KEY,
                service_id       INTEGER NOT NULL,
                name             TEXT    DEFAULT '',
                check_type       TEXT    NOT NULL,
                url              TEXT    NOT NULL,
                check_interval   INTEGER NOT NULL,
                status           TEXT    DEFAULT 'idle',
                next_check_time  INTEGER DEFAULT 0,
                processing_started_at INTEGER DEFAULT 0,
                disabled         INTEGER DEFAULT 0,
                data             TEXT    DEFAULT '{}'
            );
            CREATE TABLE check_notification_state (
                check_id           INTEGER PRIMARY KEY,
                failure_count      INTEGER NOT NULL DEFAULT 0,
                last_attempt_count INTEGER NOT NULL DEFAULT 0,
                last_immediate_at  INTEGER NOT NULL DEFAULT 0,
                last_notified_at   INTEGER NOT NULL DEFAULT 0,
                first_failure_at   INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await db.execute(
            """INSERT INTO health_check
                   (id, service_id, name, check_type, url, check_interval)
               VALUES (1, 1, 'legacy', 'http', 'https://example.test/legacy', 300)"""
        )
        await db.execute(
            """INSERT INTO check_notification_state
                   (check_id, failure_count, last_attempt_count, last_immediate_at,
                    last_notified_at, first_failure_at)
               VALUES (1, 3, 3, 0, 900, 800)"""
        )
        await db.commit()


@pytest.mark.anyio
async def test_worker_upgrades_a_pre_upgrade_table_idempotently(temp_db) -> None:
    await _create_pre_upgrade_schema(temp_db)

    for _ in range(2):
        repo = SqliteCheckRepository(temp_db)
        async with aiosqlite.connect(temp_db) as db:
            await repo._ensure_schema(db)

    with sqlite3.connect(temp_db) as connection:
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(check_notification_state)"
            ).fetchall()
        ]

    assert columns == [
        "check_id",
        "failure_count",
        "last_attempt_count",
        "last_immediate_at",
        "last_notified_at",
        "first_failure_at",
        "held_since",
        "attempt_seq",
        "attempt_at",
    ]
    # The pre-existing streak is untouched: adding zero-default columns must not
    # re-stamp last_notified_at and re-page the incident.
    assert _state_row(temp_db, 1) == (3, 3, 0, 900, 800, 0, 0, 0)


@pytest.mark.anyio
async def test_the_upgrade_does_not_claim_a_streak_adoption(temp_db, caplog) -> None:
    await _create_pre_upgrade_schema(temp_db)
    repo = SqliteCheckRepository(temp_db)

    with caplog.at_level("INFO", logger="nyxmon.adapters.repositories.sqlite_repo"):
        async with aiosqlite.connect(temp_db) as db:
            await repo._ensure_schema(db)

    [message] = [
        record.getMessage()
        for record in caplog.records
        if "check_notification_state" in record.getMessage()
    ]
    assert "held_since, attempt_seq, attempt_at" in message
    assert "last_notified_at" not in message
    assert "streaks adopted" not in message


@pytest.mark.anyio
async def test_state_reads_tolerate_a_five_column_row(temp_db) -> None:
    """``from_row`` pads a row read before the columns were added."""
    await _create_pre_upgrade_schema(temp_db)

    async with aiosqlite.connect(temp_db) as db:
        cursor = await db.execute(
            "SELECT failure_count, last_attempt_count, last_immediate_at, "
            "last_notified_at, first_failure_at FROM check_notification_state "
            "WHERE check_id = 1"
        )
        state = NotificationState.from_row(await cursor.fetchone())

    assert state == NotificationState(3, 3, 0, 900, 800, 0, 0, 0)
