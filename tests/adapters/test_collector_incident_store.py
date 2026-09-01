"""Persisted, deduplicated collector-level incidents.

This is the store-side hook the collector uses instead of forcing a per-check
alert for every reclaimed check: one incident, deduplicated across iterations
*and* process restarts, with elapsed-time reminders.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from anyio.from_thread import BlockingPortalProvider

from nyxmon.adapters.repositories import InMemoryStore, SqliteStore


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = Path(handle.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _stores(temp_db, portal_provider):
    sqlite_store = SqliteStore(temp_db)
    sqlite_store.set_portal_provider(portal_provider)
    return sqlite_store


def _exercise(store) -> None:
    assert store.get_collector_incident("stale_batch") is None

    first = store.claim_collector_incident_alert(
        "stale_batch",
        now=1_000,
        reminder_seconds=3600,
        payload={"check_ids": [1, 2, 3]},
    )
    assert first.should_notify is True
    assert first.is_new is True
    assert first.incident.opened_at == 1_000

    # Same incident on the next collector iteration: deduplicated, no alert.
    for tick in (1_001, 1_500, 4_599):
        repeat = store.claim_collector_incident_alert(
            "stale_batch", now=tick, reminder_seconds=3600
        )
        assert repeat.should_notify is False
        assert repeat.is_new is False
        assert repeat.incident.opened_at == 1_000
        assert repeat.incident.payload == {"check_ids": [1, 2, 3]}

    reminder = store.claim_collector_incident_alert(
        "stale_batch", now=4_600, reminder_seconds=3600
    )
    assert reminder.should_notify is True
    assert reminder.is_new is False
    assert reminder.incident.alert_count == 2
    assert reminder.incident.opened_at == 1_000

    closed = store.close_collector_incident("stale_batch")
    assert closed is not None
    assert closed.alert_count == 2
    assert store.get_collector_incident("stale_batch") is None
    assert store.close_collector_incident("stale_batch") is None

    # A later recurrence is a genuinely new incident and alerts again.
    again = store.claim_collector_incident_alert(
        "stale_batch", now=4_700, reminder_seconds=3600
    )
    assert again.should_notify is True
    assert again.is_new is True


def test_in_memory_collector_incident_lifecycle() -> None:
    _exercise(InMemoryStore())


def test_sqlite_collector_incident_lifecycle(temp_db) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        _exercise(_stores(temp_db, portal_provider))


def test_collector_incident_dedup_survives_a_restart(temp_db) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        opened = store.claim_collector_incident_alert(
            "collector_paused", now=1_000, reminder_seconds=3600
        )
        assert opened.should_notify is True

        # --- simulated service restart: fresh store objects, same file ---
        restarted = SqliteStore(temp_db)
        restarted.set_portal_provider(portal_provider)

        surviving = restarted.get_collector_incident("collector_paused")
        assert surviving is not None
        assert surviving.opened_at == 1_000

        after_restart = restarted.claim_collector_incident_alert(
            "collector_paused", now=1_010, reminder_seconds=3600
        )
        assert after_restart.should_notify is False
        assert after_restart.is_new is False

        later = restarted.claim_collector_incident_alert(
            "collector_paused", now=4_600, reminder_seconds=3600
        )
        assert later.should_notify is True


def test_in_memory_incidents_are_shared_with_forked_units_of_work() -> None:
    store = InMemoryStore()
    store.claim_collector_incident_alert(
        "stale_batch", now=1_000, reminder_seconds=3600
    )
    forked = store.fork_for_concurrent_uow()

    repeat = forked.claim_collector_incident_alert(
        "stale_batch", now=1_100, reminder_seconds=3600
    )

    assert repeat.should_notify is False
    assert repeat.is_new is False
    assert store.get_collector_incident("stale_batch") is not None
