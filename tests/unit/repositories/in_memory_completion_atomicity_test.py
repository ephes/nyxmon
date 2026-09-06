"""A completion in the in-memory store is one critical section.

``persist_check_result`` released the processing claim first and wrote the
notification state (and with it ``held_since``) afterwards, holding no lock.
The site connectivity observer runs in its own thread and decides what to
recheck from exactly two reads: how many executions are still in flight, and
which checks are held. Between those two writes it could see zero claims *and*
an empty held set, conclude that nothing owes a recheck, and drop the recheck
obligation of a check that was held microseconds later.

SQLite gets this atomicity from its single ``BEGIN IMMEDIATE`` transaction.
This test pins the equivalent guarantee for the in-memory store: the observer
must always see either "a claim is in flight" or "the check is held".
"""

from __future__ import annotations

import threading

import anyio

from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.adapters.repositories.in_memory import InMemoryCheckRepository
from nyxmon.adapters.repositories.interface import NotificationState
from nyxmon.domain import Check, CheckStatus, CheckType, Result, ResultStatus

CLAIMED_AT = 1_700_000_000
HELD_SINCE = 1_700_000_050

#: Bounded wait used to detect a lost critical section, not a timing
#: assumption: with the lock in place the observer can never finish here.
RACE_DETECT_SECONDS = 0.3


def _claimed_check() -> Check:
    return Check(
        check_id=1,
        service_id=1,
        name="held check",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        check_interval=300,
        next_check_time=CLAIMED_AT,
        processing_started_at=CLAIMED_AT,
        status=CheckStatus.PROCESSING,
        data={"site_dependency": "internet"},
    )


def _completing_check() -> Check:
    """The worker's copy of the claim, after it scheduled the next run."""
    completing = _claimed_check()
    completing.next_check_time = CLAIMED_AT + 300
    completing.status = CheckStatus.IDLE
    completing.processing_started_at = 0
    # schedule_next_check() keeps the claim identity; the store matches on it.
    completing.claim_started_at = CLAIMED_AT
    return completing


class _Observer:
    """The observer's two reads, run from its own thread."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.released = threading.Event()
        self.finished = threading.Event()
        self.claims: int | None = None
        self.held: int | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.released.wait(timeout=10)
        try:
            self.claims = anyio.run(
                self._store.checks.count_processing_claims_before_async,
                CLAIMED_AT + 1,
            )
            self.held = len(anyio.run(self._store.checks.list_held_checks_async))
        except BaseException as exc:  # pragma: no cover - reported by the test
            self.error = exc
        finally:
            self.finished.set()

    def race_against_the_completion(self) -> bool:
        """Release the observer and report whether it read mid-completion."""
        self.thread.start()
        self.released.set()
        return self.finished.wait(timeout=RACE_DETECT_SECONDS)

    def join(self) -> None:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "the observer never finished"
        assert self.error is None, self.error


def test_the_observer_never_sees_a_completion_half_written(monkeypatch) -> None:
    store = InMemoryStore()
    store.checks.add(_claimed_check())

    observer = _Observer(store)
    raced: list[bool] = []
    original = InMemoryCheckRepository.set_notification_state

    def hooked(
        self: InMemoryCheckRepository, check_id: int, state: NotificationState
    ) -> None:
        # The claim has just been released; the hold marker is not written yet.
        if not raced:
            raced.append(observer.race_against_the_completion())
        original(self, check_id, state)

    monkeypatch.setattr(InMemoryCheckRepository, "set_notification_state", hooked)

    persisted = store.persist_check_result(
        _completing_check(),
        Result(check_id=1, status=ResultStatus.ERROR, data={}),
        (
            NotificationState(),
            NotificationState(
                failure_count=1, first_failure_at=HELD_SINCE, held_since=HELD_SINCE
            ),
        ),
    )
    observer.join()

    assert persisted is True
    assert raced == [False], "the observer read inside the completion"
    assert observer.claims is not None and observer.held is not None
    # Either the claim was still in flight or the hold was already visible.
    # "Neither" is the torn read that loses the recheck obligation.
    assert observer.claims > 0 or observer.held > 0
    assert store.checks.get_notification_state(1).held_since == HELD_SINCE


def test_a_forked_unit_of_work_shares_the_store_lock() -> None:
    """Forks share the backing data, so they must share its mutex too."""
    store = InMemoryStore()

    forked = store.fork_for_concurrent_uow()

    assert forked.lock is store.lock
    assert forked.checks.lock is store.lock
