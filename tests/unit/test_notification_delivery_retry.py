"""Durable delivery intent, fenced acknowledgement and per-check retry.

Plan section 8: an alert used to be attempted exactly once, after the
compare-and-swap was committed. A failed or crashed send was then lost until
the next reminder window, six hours later. These tests pin the replacement:

* the intent (``attempt_seq``/``attempt_at``) is durable *before* any I/O;
* the acknowledgement is fenced by ``attempt_seq``, so an intervening OK sample
  or a newer attempt can never be cleared by a late acknowledgement;
* acknowledgements are written whether or not the retry knob is on, so turning
  the knob on later cannot resend what was already delivered;
* an ambiguous outcome ("Telegram accepted the request, the response was
  lost") and a crash between send and acknowledgement both repeat once, which
  is the documented at-least-once behaviour.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import (
    Check,
    CheckResult,
    CheckStatus,
    CheckType,
    Result,
    ResultStatus,
)
from nyxmon.service_layer.handlers import (
    ENV_NOTIFY_DELIVERY_RETRY_SECONDS,
    _delivery_retry_from_value,
    add_check_result,
    delivery_retry_seconds,
)
from nyxmon.service_layer.unit_of_work import UnitOfWork

T0 = 1_700_000_000
RETRY = 300


@pytest.fixture(autouse=True)
def _clear_env_cache():
    _delivery_retry_from_value.cache_clear()
    yield
    _delivery_retry_from_value.cache_clear()


class Clock:
    """Explicit, injectable clock: no sleeps, no wall-clock reads."""

    def __init__(self, start: int = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> None:
        self.now += seconds


class ScriptedNotifier:
    """Records every send and reports a scripted delivery outcome."""

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self.calls: list[tuple[Check, Result]] = []
        self.states_at_send: list[Any] = []
        self.outcome: bool | None | Callable[[int], bool | None] = True

    def notify_check_failed(self, check: Check, result: Result) -> bool | None:
        self.calls.append((check, result))
        if self.store is not None:
            self.states_at_send.append(
                self.store.checks.get_notification_state(check.check_id)
            )
        outcome = self.outcome
        if callable(outcome):
            return outcome(len(self.calls))
        return outcome

    def notify_service_status_changed(self, service: Any, status: str) -> None:
        del service, status

    @property
    def count(self) -> int:
        return len(self.calls)


def _build_check(check_id: int = 1, *, interval: int = 300) -> Check:
    return Check(
        check_id=check_id,
        service_id=1,
        name=f"Check {check_id}",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        check_interval=interval,
        data={},
    )


def _submit(
    uow: UnitOfWork, notifier: ScriptedNotifier, check: Check, status: str
) -> bool:
    result = Result(check_id=check.check_id, status=status, data={})
    return add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
    )


@pytest.fixture
def failing_check(monkeypatch):
    """A check that pages on every failing sample, with a frozen clock."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400")
    monkeypatch.delenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, raising=False)
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    store = InMemoryStore()
    check = _build_check()
    store.checks.add(check)
    notifier = ScriptedNotifier(store)
    return store, UnitOfWork(store=store), notifier, check, clock


# ---------------------------------------------------------------- the knob


def test_delivery_retry_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, raising=False)
    assert delivery_retry_seconds() == 0


@pytest.mark.parametrize("value", ["not-a-number", "30", "3601", "-5"])
def test_invalid_retry_values_warn_once_and_use_the_default(
    monkeypatch, caplog, value: str
) -> None:
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, value)
    with caplog.at_level("WARNING"):
        assert delivery_retry_seconds() == 0
        assert delivery_retry_seconds() == 0
    assert caplog.text.count(ENV_NOTIFY_DELIVERY_RETRY_SECONDS) == 1


def test_valid_retry_values_are_accepted(monkeypatch) -> None:
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, "300")
    assert delivery_retry_seconds() == 300


# ------------------------------------------------------- intent before I/O


def test_intent_is_persisted_before_the_notifier_is_called(failing_check) -> None:
    """A crash after the commit must leave a durable "may not have arrived"."""
    store, uow, notifier, check, clock = failing_check

    _submit(uow, notifier, check, ResultStatus.ERROR)

    assert notifier.count == 1
    # The state the notifier saw while it was sending, i.e. what a crash during
    # the send would have left behind.
    at_send = notifier.states_at_send[0]
    assert at_send.attempt_seq == 1
    assert at_send.attempt_at == clock.now

    # The successful send is acknowledged; the sequence number is kept as a
    # monotonic fence.
    after = store.checks.get_notification_state(1)
    assert after.attempt_seq == 1
    assert after.attempt_at == 0


def test_a_failed_send_leaves_the_intent_pending(failing_check) -> None:
    store, uow, notifier, check, _clock = failing_check
    notifier.outcome = False

    _submit(uow, notifier, check, ResultStatus.ERROR)

    state = store.checks.get_notification_state(1)
    assert state.attempt_seq == 1
    assert state.attempt_at == T0


def test_a_notifier_returning_none_counts_as_delivered(failing_check) -> None:
    store, uow, notifier, check, _clock = failing_check
    notifier.outcome = None

    _submit(uow, notifier, check, ResultStatus.ERROR)

    assert store.checks.get_notification_state(1).attempt_at == 0


# ---------------------------------------------------------------- fencing


def test_acknowledgement_is_fenced_by_the_attempt_sequence(failing_check) -> None:
    """failure -> OK -> new alert in the same second must not be mis-cleared."""
    store, uow, notifier, check, clock = failing_check
    notifier.outcome = False

    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert store.checks.get_notification_state(1).attempt_seq == 1

    # Recovery clears the incident but keeps the monotonic fence.
    _submit(uow, notifier, check, ResultStatus.OK)
    recovered = store.checks.get_notification_state(1)
    assert recovered.attempt_at == 0
    assert recovered.attempt_seq == 1

    # A new incident in the very same second gets its own sequence number.
    notifier.outcome = False
    _submit(uow, notifier, check, ResultStatus.ERROR)
    pending = store.checks.get_notification_state(1)
    assert pending.attempt_seq == 2
    assert pending.attempt_at == clock.now

    # A late acknowledgement of the first attempt matches nothing.
    assert store.checks.acknowledge_notification_attempt(1, 1) is False
    assert store.checks.get_notification_state(1).attempt_at == clock.now


def test_an_ok_sample_clears_a_pending_intent(failing_check, monkeypatch) -> None:
    """A failure that recovered before delivery was possible is never reported."""
    store, uow, notifier, check, clock = failing_check
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    notifier.outcome = False

    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert notifier.count == 1

    clock.advance(RETRY + 1)
    notifier.outcome = True
    _submit(uow, notifier, check, ResultStatus.OK)

    clock.advance(RETRY + 1)
    _submit(uow, notifier, check, ResultStatus.OK)

    assert notifier.count == 1
    assert store.checks.get_notification_state(1).attempt_at == 0


# ------------------------------------------------------------------ retry


def test_ambiguous_outcome_repeats_once_then_stops(failing_check, monkeypatch) -> None:
    """Telegram accepted the request but the response was lost (plan 14)."""
    store, uow, notifier, check, clock = failing_check
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    notifier.outcome = lambda call: call != 1  # only the first send "fails"

    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert notifier.count == 1

    # Inside the retry interval: no repeat, and the reminder window is a day.
    clock.advance(RETRY - 1)
    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert notifier.count == 1

    # The retry interval has elapsed: exactly one repeat, and it is delivered.
    clock.advance(1)
    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert notifier.count == 2
    assert store.checks.get_notification_state(1).attempt_at == 0

    # Delivered and acknowledged: no further repeats, ever.
    for _ in range(5):
        clock.advance(RETRY + 1)
        _submit(uow, notifier, check, ResultStatus.ERROR)
    assert notifier.count == 2


def test_the_knob_at_zero_keeps_todays_behaviour(failing_check) -> None:
    store, uow, notifier, check, clock = failing_check
    notifier.outcome = False

    _submit(uow, notifier, check, ResultStatus.ERROR)
    for _ in range(10):
        clock.advance(RETRY * 2)
        _submit(uow, notifier, check, ResultStatus.ERROR)

    # Lost until the (one day) reminder window, exactly as before the change.
    assert notifier.count == 1
    assert store.checks.get_notification_state(1).attempt_at == T0


def test_acks_written_with_the_knob_off_prevent_a_later_resend(
    failing_check, monkeypatch
) -> None:
    """Enabling the retry later must not resend an already delivered alert."""
    store, uow, notifier, check, clock = failing_check

    _submit(uow, notifier, check, ResultStatus.ERROR)
    assert notifier.count == 1
    assert store.checks.get_notification_state(1).attempt_at == 0

    # The operator turns the retry on during the incident.
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    for _ in range(5):
        clock.advance(RETRY + 1)
        _submit(uow, notifier, check, ResultStatus.ERROR)

    assert notifier.count == 1


def test_the_retry_ignores_the_reminder_window(failing_check, monkeypatch) -> None:
    store, uow, notifier, check, clock = failing_check
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    notifier.outcome = False

    _submit(uow, notifier, check, ResultStatus.ERROR)
    epochs = []
    for _ in range(3):
        clock.advance(RETRY)
        _submit(uow, notifier, check, ResultStatus.ERROR)
        epochs.append(clock.now)

    # One send per retry interval although the reminder window is 24 hours.
    assert notifier.count == 4
    assert store.checks.get_notification_state(1).attempt_seq == 4
    assert store.checks.get_notification_state(1).attempt_at == epochs[-1]


# ------------------------------------------------ immediate alerts


def _submit_immediate(
    uow: UnitOfWork, notifier: ScriptedNotifier, check: Check
) -> bool:
    """A ``notification_immediate`` failing sample, which skips the streak."""
    result = Result(
        check_id=check.check_id,
        status=ResultStatus.ERROR,
        data={"notification_immediate": True},
    )
    return add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
    )


def test_an_unacknowledged_immediate_alert_is_repeated_inside_the_cooldown(
    failing_check, monkeypatch
) -> None:
    """The immediate cooldown bounds new alerts, not a redelivery.

    ``notification_immediate`` returned before the delivery-retry rule was
    reached, so an immediate alert whose send failed stayed unacknowledged for
    the whole one-hour cooldown even with the retry explicitly enabled - the
    one class of alert that exists because it must not wait.
    """
    store, uow, notifier, check, clock = failing_check
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    notifier.outcome = False

    _submit_immediate(uow, notifier, check)
    assert notifier.count == 1
    assert store.checks.get_notification_state(1).attempt_at == T0

    # Well inside the one-hour cooldown, but the retry interval has elapsed.
    clock.advance(RETRY)
    _submit_immediate(uow, notifier, check)

    assert notifier.count == 2
    pending = store.checks.get_notification_state(1)
    assert pending.attempt_seq == 2
    assert pending.attempt_at == clock.now

    # The repeat is delivered, so the intent is cleared and the cooldown rules
    # again: no further alert until it expires.
    notifier.outcome = True
    clock.advance(RETRY)
    _submit_immediate(uow, notifier, check)
    assert notifier.count == 3
    assert store.checks.get_notification_state(1).attempt_at == 0

    for _ in range(3):
        clock.advance(RETRY)
        _submit_immediate(uow, notifier, check)
    assert notifier.count == 3


def test_the_immediate_cooldown_is_untouched_with_the_knob_off(
    failing_check,
) -> None:
    """Default configuration: a failed immediate send waits out the cooldown."""
    store, uow, notifier, check, clock = failing_check
    notifier.outcome = False

    _submit_immediate(uow, notifier, check)
    assert notifier.count == 1

    for _ in range(5):
        clock.advance(RETRY)
        _submit_immediate(uow, notifier, check)

    assert notifier.count == 1
    assert store.checks.get_notification_state(1).attempt_at == T0


# -------------------------------------------------------- restart recovery


@pytest.mark.anyio
async def test_crash_between_send_and_acknowledgement_retries_after_restart(
    monkeypatch, tmp_path
) -> None:
    """The intent is durable, so a lost acknowledgement costs one duplicate."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400")
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    clock = Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    db_path = tmp_path / "nyxmon.sqlite3"

    # --- process 1: the alert is sent, the process dies before the ack ---
    store = SqliteStore(db_path)
    from anyio.from_thread import BlockingPortalProvider

    provider = BlockingPortalProvider()
    store.set_portal_provider(provider)
    check = _build_check()
    check.status = CheckStatus.IDLE
    await store.checks._add_async(check)
    notifier = ScriptedNotifier()

    def crash(_check_id: int, _attempt_seq: int) -> bool:
        raise RuntimeError("the process died before acknowledging")

    monkeypatch.setattr(store.checks, "acknowledge_notification_attempt", crash)
    _submit(UnitOfWork(store=store), notifier, check, ResultStatus.ERROR)
    assert notifier.count == 1

    persisted = await store.checks._get_notification_state_async(1)
    assert persisted.attempt_seq == 1
    assert persisted.attempt_at == T0, (
        "the intent was not durable, so a restart cannot retry the alert"
    )

    # --- process 2: fresh store over the same file, delivery works ---
    restarted = SqliteStore(db_path)
    restarted.set_portal_provider(provider)
    restarted_notifier = ScriptedNotifier()
    clock.advance(RETRY + 1)
    _submit(
        UnitOfWork(store=restarted),
        restarted_notifier,
        _build_check(),
        ResultStatus.ERROR,
    )

    assert restarted_notifier.count == 1, "the pending alert was never retried"
    acknowledged = await restarted.checks._get_notification_state_async(1)
    assert acknowledged.attempt_at == 0
    assert acknowledged.attempt_seq == 2
