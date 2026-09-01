import logging
import os
from functools import lru_cache
from time import time as current_epoch
from typing import Callable

from anyio.from_thread import BlockingPortalProvider

from ..adapters.collector import CheckCollector
from ..adapters.cleaner import ResultsCleaner
from ..adapters.notification import Notifier
from ..adapters.repositories.interface import (
    NotificationState,
    NotificationStateConflict,
)
from ..domain import events, commands
from ..domain.models import CheckResult, ResultStatus
from ..adapters.runner import CheckRunner
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

__all__ = [
    "DEFAULT_NOTIFY_CONSECUTIVE_FAILURES",
    "DEFAULT_NOTIFY_REPEAT_INTERVAL_SECONDS",
    "DEFAULT_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS",
    "DEFAULT_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS",
    "MAX_NOTIFICATION_STATE_ATTEMPTS",
    "add_check_result",
    "COMMAND_HANDLERS",
    "EVENT_HANDLERS",
]

logger = logging.getLogger(__name__)


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


def _should_notify_check_result(
    check_result: CheckResult,
    uow: UnitOfWork,
    immediate_cooldown_seconds: int,
) -> tuple[bool, NotificationState, NotificationState]:
    """Decide whether this sample should produce an external notification.

    Returns ``(should_notify, expected_state, next_state)``. The two states form
    the compare-and-swap transition handed to
    :meth:`RepositoryStore.persist_check_result`; the whole record is compared,
    so a concurrent writer can never be silently overwritten.
    """
    check = check_result.check
    state = uow.store.checks.get_notification_state(check.check_id)
    if check_result.result.status == ResultStatus.OK:
        # Recovery closes the incident: streak, reminder timing and the
        # immediate-alert cooldown all reset, so a later failure is a genuinely
        # new incident and alerts again on its own merits.
        return False, state, state.cleared()

    if not check_result.should_notify:
        return False, state, state

    if check_result.force_notification:
        return True, state, state

    if check_result.result.data.get("collector_internal"):
        # Collector-internal bookkeeping (an expired processing lease). The
        # endpoint itself was never observed, so this sample is recorded in the
        # result history but is deliberately inert for alerting: it never pages
        # on its own - not even under a per-check ``consecutive_failures: 1``
        # policy - and it neither advances nor resets the check's own incident.
        # Reclaiming N checks is reported once, at collector level.
        return False, state, state

    if check_result.result.data.get("notification_suppressed"):
        # Maintenance windows keep the result history but break the incident.
        return False, state, state.with_streak_reset()

    if check_result.result.data.get("notification_immediate"):
        now = int(current_epoch())
        should_notify = now - state.last_immediate_at >= immediate_cooldown_seconds
        return (
            should_notify,
            state,
            state.evolve(
                last_immediate_at=now if should_notify else state.last_immediate_at
            ),
        )

    now = int(current_epoch())
    policy = resolve_notification_policy(check, check_result.result.status)
    failure_count = state.failure_count + 1
    first_failure_at = state.first_failure_at or now

    if state.last_notified_at == 0:
        # No alert yet for this incident: apply the initial-alert threshold.
        should_notify = failure_count >= policy.consecutive_failures
    else:
        # Open incident: remind on elapsed wall-clock time, never sample count.
        should_notify = now - state.last_notified_at >= policy.reminder_seconds

    return (
        should_notify,
        state,
        NotificationState(
            failure_count=failure_count,
            last_attempt_count=(
                failure_count if should_notify else state.last_attempt_count
            ),
            last_immediate_at=state.last_immediate_at,
            last_notified_at=now if should_notify else state.last_notified_at,
            first_failure_at=first_failure_at,
        ),
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
    cmd: commands.AddCheckResult, uow: UnitOfWork, notifier: Notifier
) -> bool:
    """Add a check to the repository and trigger notifications if needed."""
    check_result = cmd.check_result
    check, result = check_result.check, check_result.result
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
    should_notify = False
    for attempt in range(MAX_NOTIFICATION_STATE_ATTEMPTS):
        should_notify, previous_state, notification_state = _should_notify_check_result(
            check_result,
            uow,
            _notify_immediate_cooldown_seconds(),
        )
        transition = (previous_state, notification_state)
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
            break
        except NotificationStateConflict:
            if attempt == MAX_NOTIFICATION_STATE_ATTEMPTS - 1:
                logger.error(
                    "notification state remained contended for check_id=%s; "
                    "persisting result without changing alert state",
                    check.check_id,
                )
                with uow:
                    uow.store.persist_check_result(
                        check,
                        result,
                        None,
                        complete_check=not check_result.force_notification,
                    )
                    uow.commit()
                should_notify = False
                break
            logger.warning(
                "notification state changed concurrently for check_id=%s; retrying",
                check.check_id,
            )

    # Persist every sample, but only trigger side effects when the failure streak
    # crosses the configured threshold or the elapsed reminder window expires.
    if should_notify:
        notifier.notify_check_failed(check, result)
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
