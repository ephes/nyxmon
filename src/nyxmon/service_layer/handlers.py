import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from time import time as current_epoch
from typing import Any, Callable

from anyio.from_thread import BlockingPortalProvider

from ..adapters.collector import CheckCollector
from ..adapters.cleaner import ResultsCleaner
from ..adapters.notification import Notifier
from ..adapters.repositories.interface import (
    NotificationState,
    NotificationStateConflict,
)
from ..adapters.site_connectivity import (
    NullSiteState,
    SiteConnectivityConfig,
    SiteMode,
    SiteStateProvider,
)
from ..domain import events, commands
from ..domain.models import CheckResult, ResultStatus
from ..adapters.runner import CheckRunner
from .site_dependency import resolve_site_dependency
from .unit_of_work import UnitOfWork
from ..domain.commands import AddCheckResult
from .notification_suppression import notification_suppression_details
from .notification_policy import (
    DEFAULT_NOTIFY_CONSECUTIVE_FAILURES,
    DEFAULT_NOTIFY_REPEAT_INTERVAL_SECONDS,
    DEFAULT_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS,
    resolve_notification_policy,
)


DEFAULT_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS = 3600
MAX_NOTIFICATION_STATE_ATTEMPTS = 3

# Per-check delivery retry is off by default (plan section 8 and 11): with the
# knob at 0 a failed send behaves exactly as before, and enabling it later
# cannot resend alerts that were already delivered while it was off.
DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS = 0
MIN_NOTIFY_DELIVERY_RETRY_SECONDS = 60
MAX_NOTIFY_DELIVERY_RETRY_SECONDS = 3600
ENV_NOTIFY_DELIVERY_RETRY_SECONDS = "NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS"

#: Key under ``result.data`` carrying why a sample was (or would have been) held.
SITE_CONNECTIVITY_DATA_KEY = "site_connectivity"

__all__ = [
    "DEFAULT_NOTIFY_CONSECUTIVE_FAILURES",
    "DEFAULT_NOTIFY_REPEAT_INTERVAL_SECONDS",
    "DEFAULT_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS",
    "DEFAULT_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS",
    "DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS",
    "ENV_NOTIFY_DELIVERY_RETRY_SECONDS",
    "MAX_NOTIFICATION_STATE_ATTEMPTS",
    "SITE_CONNECTIVITY_DATA_KEY",
    "add_check_result",
    "delivery_retry_seconds",
    "COMMAND_HANDLERS",
    "EVENT_HANDLERS",
]

logger = logging.getLogger(__name__)

#: Used when the site state provider carries no configuration of its own
#: (``NullSiteState``, stubs), so the hold bounds are always defined.
_DEFAULT_SITE_CONFIG = SiteConnectivityConfig()


@lru_cache(maxsize=None)
def _positive_env_value(value: str, default: int, env_name: str) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "%s is invalid; using default %s",
            env_name,
            default,
        )
        return default
    if parsed <= 0:
        logger.warning(
            "%s must be positive; using default %s",
            env_name,
            default,
        )
        return default
    return parsed


def _notify_immediate_cooldown_seconds() -> int:
    value = os.environ.get("NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS", "").strip()
    return _positive_env_value(
        value,
        DEFAULT_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS,
        "NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS",
    )


@lru_cache(maxsize=None)
def _delivery_retry_from_value(value: str) -> int:
    """Validate ``NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS`` (0, or 60-3600)."""
    if not value:
        return DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "%s is invalid; using default %s",
            ENV_NOTIFY_DELIVERY_RETRY_SECONDS,
            DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS,
        )
        return DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS
    if parsed == 0:
        return 0
    if (
        parsed < MIN_NOTIFY_DELIVERY_RETRY_SECONDS
        or parsed > MAX_NOTIFY_DELIVERY_RETRY_SECONDS
    ):
        logger.warning(
            "%s must be 0 or between %s and %s; using default %s",
            ENV_NOTIFY_DELIVERY_RETRY_SECONDS,
            MIN_NOTIFY_DELIVERY_RETRY_SECONDS,
            MAX_NOTIFY_DELIVERY_RETRY_SECONDS,
            DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS,
        )
        return DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS
    return parsed


def delivery_retry_seconds() -> int:
    """How long an unacknowledged alert waits before it is sent again.

    Returns:
        ``0`` when the per-check delivery retry is switched off, which is the
        default and reproduces the behaviour before the site connectivity work.
    """
    value = os.environ.get(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, "").strip()
    return _delivery_retry_from_value(value)


def _site_config(site_state: SiteStateProvider | None) -> SiteConnectivityConfig:
    """The configuration bounding holds, taken from the provider when it has one."""
    config = getattr(site_state, "config", None)
    if isinstance(config, SiteConnectivityConfig):
        return config
    return _DEFAULT_SITE_CONFIG


@dataclass(frozen=True, slots=True)
class _NotificationDecision:
    """What one sample does to the notification state.

    Attributes:
        should_notify: Whether the notifier must be called for this sample.
        expected: The state the transition was calculated from.
        next_state: The state to swap in.
        held: Whether the sample's notification was deferred by a site
            dependency. The conflict-exhaustion fallback needs this to stamp
            the hold marker in the same transaction as the completion.
        attempt_seq: The attempt sequence stamped by this transition, ``0``
            when it stamped none. Only a stamped attempt may be acknowledged.
        now: The epoch the decision was taken at.
    """

    should_notify: bool
    expected: NotificationState
    next_state: NotificationState
    held: bool = False
    attempt_seq: int = 0
    now: int = 0


def _with_attempt(state: NotificationState, now: int) -> NotificationState:
    """Record the durable intent to notify, before any I/O (plan section 8)."""
    return state.evolve(attempt_seq=state.attempt_seq + 1, attempt_at=now)


def _annotate_site_connectivity(
    check_result: CheckResult, *, held: bool, reason: dict[str, Any]
) -> None:
    """Attach the hold reason to the stored sample, like ``notification_suppressed``."""
    check_result.result.data = {
        **check_result.result.data,
        SITE_CONNECTIVITY_DATA_KEY: {"held": held, **reason},
    }


def _should_notify_check_result(
    check_result: CheckResult,
    uow: UnitOfWork,
    immediate_cooldown_seconds: int,
    *,
    site_state: SiteStateProvider | None = None,
    retry_seconds: int = 0,
) -> _NotificationDecision:
    """Decide whether this sample should produce an external notification.

    The returned expected/next states form the compare-and-swap transition
    handed to :meth:`RepositoryStore.persist_check_result`; the whole record is
    compared, so a concurrent writer can never be silently overwritten.

    Args:
        check_result: The sample and its check.
        uow: Unit of work whose store holds the notification state.
        immediate_cooldown_seconds: Cooldown for ``notification_immediate``.
        site_state: Provider of the connectivity snapshot; ``None`` and
            :class:`NullSiteState` both mean "nothing is ever held".
        retry_seconds: Delivery retry interval, ``0`` when the retry is off.

    Returns:
        The decision, including whether the sample was held and which attempt
        sequence, if any, this transition stamped.
    """
    check = check_result.check
    result = check_result.result
    state = uow.store.checks.get_notification_state(check.check_id)
    if result.status == ResultStatus.OK:
        # Recovery closes the incident: streak, reminder timing, the hold
        # budget, the pending delivery intent and the immediate-alert cooldown
        # all reset, so a later failure is a genuinely new incident and alerts
        # again on its own merits.
        return _NotificationDecision(False, state, state.cleared())

    if not check_result.should_notify:
        return _NotificationDecision(False, state, state)

    if check_result.force_notification:
        # The collector-internal path pages by construction; it is never held
        # and keeps no per-check delivery intent.
        return _NotificationDecision(True, state, state)

    if result.data.get("collector_internal"):
        # Collector-internal bookkeeping (an expired processing lease). The
        # endpoint itself was never observed, so this sample is recorded in the
        # result history but is deliberately inert for alerting: it never pages
        # on its own - not even under a per-check ``consecutive_failures: 1``
        # policy - and it neither advances nor resets the check's own incident.
        # Reclaiming N checks is reported once, at collector level.
        return _NotificationDecision(False, state, state)

    if result.data.get("notification_suppressed"):
        # Maintenance windows keep the result history but break the incident.
        # Only the incident: the hold marker and a pending delivery intent
        # survive, so a suppressed sample during an outage can neither re-arm
        # the hold budget (which would also drop the check from the observer's
        # recheck set) nor cancel an alert nobody has acknowledged.
        return _NotificationDecision(False, state, state.with_streak_reset())

    now = int(current_epoch())
    config = _site_config(site_state)
    stale_after = config.stale_after_seconds
    max_hold_seconds = config.max_hold_seconds
    dependency = resolve_site_dependency(check)
    snapshot = site_state.snapshot() if site_state is not None else None
    hold: dict[str, Any] | None = None
    recovered = False
    if dependency is not None and snapshot is not None:
        hold = snapshot.hold_reason(
            dependency, check.claim_started_at, now, stale_after=stale_after
        )
        recovered = snapshot.dependency_recovered(
            dependency, check.claim_started_at, now, stale_after=stale_after
        )
        if hold is None and snapshot.mode is SiteMode.OBSERVE:
            # Observe mode records the judgement without acting on it, so the
            # observer can be verified before it may suppress anything.
            observed = snapshot.observed_reason(
                dependency, check.claim_started_at, now, stale_after=stale_after
            )
            if observed is not None:
                _annotate_site_connectivity(check_result, held=False, reason=observed)

    # A hold is bounded: after ``max_hold_seconds`` the check follows ordinary
    # policy again for the rest of the incident, and the budget is not re-armed
    # until the dependency is observed recovered.
    exhausted = state.held_since > 0 and now - state.held_since >= max_hold_seconds
    if hold is not None and not exhausted:
        _annotate_site_connectivity(check_result, held=True, reason=hold)
        return _NotificationDecision(
            False,
            state,
            state.evolve(
                failure_count=state.failure_count + 1,
                first_failure_at=state.first_failure_at or now,
                held_since=state.held_since or now,
            ),
            held=True,
            now=now,
        )
    if hold is not None:
        _annotate_site_connectivity(
            check_result, held=False, reason={**hold, "exhausted": True}
        )

    # ``held_since`` is re-armed only by an observed recovery on a fresh
    # snapshot. A bypass because the snapshot is stale or the budget is spent
    # leaves it untouched, so a persisting outage cannot restart the budget.
    held_since = 0 if recovered else state.held_since

    # A pending, unacknowledged attempt is repeated whatever branch claimed it
    # (plan section 8): the previous send failed or its outcome was ambiguous,
    # and losing the incident is worse than a duplicate. Site holds are decided
    # above, so a held sample is still never repeated.
    retry_due = (
        retry_seconds > 0
        and state.attempt_at > 0
        and now - state.attempt_at >= retry_seconds
    )

    if result.data.get("notification_immediate"):
        # The immediate cooldown bounds new alerts, not the redelivery of one
        # that may never have arrived.
        should_notify = (
            retry_due or now - state.last_immediate_at >= immediate_cooldown_seconds
        )
        next_state = state.evolve(
            last_immediate_at=now if should_notify else state.last_immediate_at,
            held_since=held_since,
        )
        if should_notify:
            next_state = _with_attempt(next_state, now)
        return _NotificationDecision(
            should_notify,
            state,
            next_state,
            attempt_seq=next_state.attempt_seq if should_notify else 0,
            now=now,
        )

    policy = resolve_notification_policy(check, result.status)
    failure_count = state.failure_count + 1
    first_failure_at = state.first_failure_at or now

    if retry_due:
        # Repeated irrespective of the reminder window.
        should_notify = True
    elif state.last_notified_at == 0:
        # No alert yet for this incident: apply the initial-alert threshold.
        should_notify = failure_count >= policy.consecutive_failures
    else:
        # Open incident: remind on elapsed wall-clock time, never sample count.
        should_notify = now - state.last_notified_at >= policy.reminder_seconds

    # Derived from the state that was read, never built from a fresh
    # NotificationState(): the compare-and-swap compares whole records, so a
    # field this function forgot to carry would be silently reset rather than
    # rejected as a conflict.
    next_state = state.evolve(
        failure_count=failure_count,
        last_attempt_count=(
            failure_count if should_notify else state.last_attempt_count
        ),
        last_notified_at=now if should_notify else state.last_notified_at,
        first_failure_at=first_failure_at,
        held_since=held_since,
    )
    if should_notify:
        next_state = _with_attempt(next_state, now)
    return _NotificationDecision(
        should_notify,
        state,
        next_state,
        attempt_seq=next_state.attempt_seq if should_notify else 0,
        now=now,
    )


def execute_checks(
    cmd: commands.ExecuteChecks, runner: CheckRunner, uow: UnitOfWork
) -> None:
    """Execute all pending checks."""
    check_by_check_id = {check.check_id: check for check in cmd.checks}

    def result_received(result):
        check = check_by_check_id[result.check_id]
        check.schedule_next_check()  # Schedule the next check after a result is received
        check_result = CheckResult(check=check, result=result)
        inner_cmd = AddCheckResult(check_result=check_result)
        uow.add_command(inner_cmd)  # add command to the unit of work

    runner.run_all(cmd.checks, result_received)


def add_check(cmd: commands.AddCheck, uow: UnitOfWork) -> None:
    """Add a check to the repository."""
    check = cmd.check
    with uow:
        uow.store.checks.add(check)
        uow.commit()


def add_check_result(
    cmd: commands.AddCheckResult,
    uow: UnitOfWork,
    notifier: Notifier,
    site_state: SiteStateProvider | None = None,
) -> bool:
    """Persist a sample and alert about it when policy and site state allow.

    Args:
        cmd: The command carrying the check and its result.
        uow: Unit of work used for the atomic persist.
        notifier: Where an alert goes. A literal ``False`` return means the
            send failed and the delivery intent stays pending.
        site_state: Provider of the connectivity snapshot. ``None`` behaves
            like :class:`NullSiteState`: nothing is ever held.

    Returns:
        Whether an external notification was triggered for this sample.
    """
    check_result = cmd.check_result
    check, result = check_result.check, check_result.result
    if site_state is None:
        site_state = NullSiteState()
    if result.status in (
        ResultStatus.ERROR,
        ResultStatus.WARNING,
    ):
        suppression_details = (
            None
            if check_result.force_notification or result.data.get("collector_internal")
            else notification_suppression_details(check)
        )
        if suppression_details:
            result.data = {
                **result.data,
                "notification_suppressed": suppression_details,
            }
    retry_seconds = delivery_retry_seconds()
    should_notify = False
    attempt_seq = 0
    for attempt in range(MAX_NOTIFICATION_STATE_ATTEMPTS):
        decision = _should_notify_check_result(
            check_result,
            uow,
            _notify_immediate_cooldown_seconds(),
            site_state=site_state,
            retry_seconds=retry_seconds,
        )
        should_notify = decision.should_notify
        attempt_seq = decision.attempt_seq
        transition = (decision.expected, decision.next_state)
        try:
            with uow:
                persisted = uow.store.persist_check_result(
                    check,
                    result,
                    transition,
                    complete_check=not check_result.force_notification,
                )
                uow.commit()
            if not persisted:
                should_notify = False
                attempt_seq = 0
            break
        except NotificationStateConflict:
            if attempt == MAX_NOTIFICATION_STATE_ATTEMPTS - 1:
                logger.error(
                    "notification state remained contended for check_id=%s; "
                    "persisting result without changing alert state",
                    check.check_id,
                )
                # The streak bookkeeping of the exhausted transition is
                # dropped, but a hold must not be: the marker lands in the same
                # transaction as the completion, so the observer can never see
                # an idle, unheld row for a claim that owes a recheck. It is
                # passed only when this sample really was held, so the ordinary
                # fallback keeps its previous call shape.
                fallback: dict[str, Any] = {
                    "complete_check": not check_result.force_notification
                }
                if decision.held:
                    fallback["hold_marker"] = decision.now
                with uow:
                    uow.store.persist_check_result(check, result, None, **fallback)
                    uow.commit()
                should_notify = False
                attempt_seq = 0
                break
            logger.warning(
                "notification state changed concurrently for check_id=%s; retrying",
                check.check_id,
            )

    # Persist every sample, but only trigger side effects when the failure streak
    # crosses the configured threshold or the elapsed reminder window expires.
    if should_notify:
        delivered = notifier.notify_check_failed(check, result)
        if delivered is not False and attempt_seq > 0:
            # Acknowledge unconditionally, independent of the retry knob: with
            # the knob off the marker is inert, and enabling it later must not
            # resend an alert that was already delivered.
            try:
                uow.store.checks.acknowledge_notification_attempt(
                    check.check_id, attempt_seq
                )
            except Exception:
                logger.exception(
                    "failed to acknowledge notification attempt %s for check_id=%s",
                    attempt_seq,
                    check.check_id,
                )
    return should_notify


def start_collector(
    _cmd: commands.StartCollector,
    collector: CheckCollector,
    portal_provider: BlockingPortalProvider,
) -> None:
    """Start the check collector."""
    collector.set_portal_provider(portal_provider)
    collector.start()


def stop_collector(_cmd: commands.StopCollector, collector: CheckCollector) -> None:
    """Stop the check collector."""
    collector.stop()


def start_cleaner(
    _cmd: commands.StartCleaner,
    cleaner: ResultsCleaner,
    portal_provider: BlockingPortalProvider,
) -> None:
    """Start the results cleaner."""
    cleaner.set_portal_provider(portal_provider)
    cleaner.start()


def stop_cleaner(_cmd: commands.StopCleaner, cleaner: ResultsCleaner) -> None:
    """Stop the results cleaner."""
    cleaner.stop()


def service_status_changed(
    event: events.ServiceStatusChanged, uow: UnitOfWork, notifier: Notifier
) -> None:
    with uow:
        service = uow.store.services.get(event.service_id)
        # Notify about the service status change
        notifier.notify_service_status_changed(service, event.status)
        # Update service status
        if hasattr(service, "update_status"):
            service.update_status(event.status)
        uow.commit()


def check_failed(event: events.CheckFailed, uow: UnitOfWork) -> None:
    # This handler is called when a check fails, after the notification has already been sent
    # We could add additional actions here, like retrying the check or updating service status
    pass


def check_succeeded(event: events.CheckSucceeded, uow: UnitOfWork) -> None:
    # This handler is called when a check succeeds
    # We could add actions like resetting failure counters, etc.
    pass


EVENT_HANDLERS: dict[type[events.Event], list[Callable]] = {
    events.ServiceStatusChanged: [service_status_changed],
    events.CheckFailed: [check_failed],
    events.CheckSucceeded: [check_succeeded],
}

COMMAND_HANDLERS: dict[type[commands.Command], Callable] = {
    commands.ExecuteChecks: execute_checks,
    commands.AddCheck: add_check,
    commands.AddCheckResult: add_check_result,
    commands.StartCollector: start_collector,
    commands.StopCollector: stop_collector,
    commands.StartCleaner: start_cleaner,
    commands.StopCleaner: stop_cleaner,
}
