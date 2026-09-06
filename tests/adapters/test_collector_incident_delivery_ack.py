"""Acknowledging a collector-incident alert is one atomic operation.

``_record_incident_send`` used to read the incident row, compare
``delivery_attempt`` in the collector, and then replace the whole payload in a
second store call. A claim granted between the read and the write was silently
undone: the newer alert lost both its ``delivery_pending`` intent and the
payload it had just written, so a reminder that had already been claimed was
never retried and its incident details were rolled back to the older ones.

These tests pin the replacement contract of
``acknowledge_collector_incident_delivery``: the compare and the write share a
single critical section, and every payload key other than the two delivery
markers is read from the row instead of being supplied by the caller.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import anyio
import pytest
from anyio.from_thread import BlockingPortalProvider

from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.adapters.repositories import in_memory as in_memory_module
from nyxmon.adapters.repositories import sqlite_repo as sqlite_module

INCIDENT_KEY = "collector:stale_processing_lease"

#: How long the acknowledgement waits, mid-write, for the competing claim to
#: finish. It must not finish: this is a failure detector for a lost critical
#: section, not a timing assumption. Serialised code always hits the timeout.
RACE_DETECT_SECONDS = 0.3


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
        db_path = Path(handle.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


class _CompetingClaim:
    """A newer claim, run from another thread inside the acknowledgement."""

    def __init__(self, claim: Callable[[], Any]) -> None:
        self._claim = claim
        self.finished = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self._claim()
        except BaseException as exc:  # pragma: no cover - reported by the test
            self.error = exc
        finally:
            self.finished.set()

    def run_inside_acknowledgement(self) -> bool:
        """Start the claim and report whether it completed before we did.

        Returns:
            ``True`` when the claim finished while the acknowledgement was
            still mid-write, which is exactly the interleaving the atomic
            operation must make impossible.
        """
        self.thread.start()
        return self.finished.wait(timeout=RACE_DETECT_SECONDS)

    def join(self) -> None:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "the competing claim never finished"
        assert self.error is None, self.error


def _hook_between_read_and_write(
    monkeypatch, module, competition: _CompetingClaim, raced: list[bool]
) -> None:
    """Run the competing claim once, between the ack's read and its write."""
    original = module.without_delivery_markers

    def hook(payload: dict[str, Any]) -> dict[str, Any]:
        if not raced:
            raced.append(competition.run_inside_acknowledgement())
        return original(payload)

    monkeypatch.setattr(module, "without_delivery_markers", hook)


def _assert_newer_intent_survived(store) -> None:
    incident = store.get_collector_incident(INCIDENT_KEY)
    assert incident is not None
    assert incident.alert_count == 2
    # The newer claim's intent AND its payload are intact: the acknowledgement
    # of attempt 1 neither cleared the markers of attempt 2 nor rolled the
    # reclaimed count back to the one the older alert reported.
    assert incident.payload == {
        "reclaimed_count": 9,
        "delivery_pending": True,
        "delivery_attempt": 2,
    }


# ------------------------------------------------------------- happy path


@pytest.mark.parametrize("store_kind", ["in_memory", "sqlite"])
def test_acknowledgement_clears_only_the_delivery_markers(
    store_kind: str, temp_db
) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        if store_kind == "sqlite":
            store: Any = SqliteStore(temp_db)
            store.set_portal_provider(portal_provider)
        else:
            store = InMemoryStore()

        store.claim_collector_incident_alert(
            INCIDENT_KEY,
            now=1_000,
            reminder_seconds=3_600,
            payload={"reclaimed_count": 3, "check_ids": [1, 2]},
        )

        assert store.acknowledge_collector_incident_delivery(INCIDENT_KEY, 1) is True

        incident = store.get_collector_incident(INCIDENT_KEY)
        assert incident is not None
        assert incident.payload == {"reclaimed_count": 3, "check_ids": [1, 2]}
        assert incident.alert_count == 1
        assert incident.last_alert_at == 1_000


@pytest.mark.parametrize("store_kind", ["in_memory", "sqlite"])
def test_acknowledgement_is_fenced_by_the_attempt(store_kind: str, temp_db) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        if store_kind == "sqlite":
            store: Any = SqliteStore(temp_db)
            store.set_portal_provider(portal_provider)
        else:
            store = InMemoryStore()

        assert store.acknowledge_collector_incident_delivery(INCIDENT_KEY, 1) is False

        store.claim_collector_incident_alert(
            INCIDENT_KEY, now=1_000, reminder_seconds=3_600, payload={"n": 1}
        )
        store.claim_collector_incident_alert(
            INCIDENT_KEY, now=9_000, reminder_seconds=3_600, payload={"n": 2}
        )

        # A late acknowledgement of the first alert must not clear the second.
        assert store.acknowledge_collector_incident_delivery(INCIDENT_KEY, 1) is False

        incident = store.get_collector_incident(INCIDENT_KEY)
        assert incident is not None
        assert incident.payload == {
            "n": 2,
            "delivery_pending": True,
            "delivery_attempt": 2,
        }


# ------------------------------------------------- the reviewer's interleaving


def test_in_memory_ack_cannot_lose_a_claim_granted_while_it_runs(monkeypatch) -> None:
    store = InMemoryStore()
    store.claim_collector_incident_alert(
        INCIDENT_KEY,
        now=1_000,
        reminder_seconds=3_600,
        payload={"reclaimed_count": 3},
    )

    competition = _CompetingClaim(
        lambda: store.claim_collector_incident_alert(
            INCIDENT_KEY,
            now=5_000,
            reminder_seconds=3_600,
            payload={"reclaimed_count": 9},
        )
    )
    raced: list[bool] = []
    _hook_between_read_and_write(monkeypatch, in_memory_module, competition, raced)

    acknowledged = store.acknowledge_collector_incident_delivery(INCIDENT_KEY, 1)
    competition.join()

    assert acknowledged is True
    assert raced == [False], "the claim ran inside the acknowledgement's write"
    _assert_newer_intent_survived(store)


def test_sqlite_ack_cannot_lose_a_claim_granted_while_it_runs(
    monkeypatch, temp_db
) -> None:
    portal_provider = BlockingPortalProvider()
    with portal_provider:
        store = SqliteStore(temp_db)
        store.set_portal_provider(portal_provider)
        store.claim_collector_incident_alert(
            INCIDENT_KEY,
            now=1_000,
            reminder_seconds=3_600,
            payload={"reclaimed_count": 3},
        )

        def competing_claim() -> None:
            # Its own event loop and its own connection: the only thing that
            # can hold it back is the acknowledgement's write transaction.
            anyio.run(
                store._claim_collector_incident_alert_async,
                INCIDENT_KEY,
                5_000,
                3_600,
                {"reclaimed_count": 9},
            )

        competition = _CompetingClaim(competing_claim)
        raced: list[bool] = []
        _hook_between_read_and_write(monkeypatch, sqlite_module, competition, raced)

        acknowledged = store.acknowledge_collector_incident_delivery(INCIDENT_KEY, 1)
        competition.join()

        assert acknowledged is True
        assert raced == [False], "the claim ran inside the acknowledgement's write"
        _assert_newer_intent_survived(store)
