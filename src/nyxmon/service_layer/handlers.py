import os
from typing import Callable

from anyio.from_thread import BlockingPortalProvider

from ..adapters.collector import CheckCollector
from ..adapters.cleaner import ResultsCleaner
from ..adapters.notification import Notifier
from ..domain import events, commands
from ..domain.models import CheckResult, ResultStatus
from ..adapters.runner import CheckRunner
from .unit_of_work import UnitOfWork
from ..domain.commands import AddCheckResult
from .notification_suppression import notification_suppression_details


DEFAULT_NOTIFY_CONSECUTIVE_FAILURES = 2


def _notify_consecutive_failure_threshold() -> int:
    value = os.environ.get("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "").strip()
    if not value:
        return DEFAULT_NOTIFY_CONSECUTIVE_FAILURES
    try:
        threshold = int(value)
    except ValueError:
        return DEFAULT_NOTIFY_CONSECUTIVE_FAILURES
    if threshold <= 0:
        return DEFAULT_NOTIFY_CONSECUTIVE_FAILURES
    return threshold


def _should_notify_check_result(
    check_result: CheckResult, uow: UnitOfWork, threshold: int
) -> bool:
    if not check_result.should_notify:
        return False

    recent_results = uow.store.results.list_for_check(
        check_result.check.check_id,
        limit=threshold + 1,
    )
    consecutive_failures = 0
    for result in recent_results:
        if result.data.get("notification_suppressed"):
            break
        if result.status in (ResultStatus.ERROR, ResultStatus.WARNING):
            consecutive_failures += 1
            continue
        break

    return consecutive_failures == threshold


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
) -> None:
    """Add a check to the repository and trigger notifications if needed."""
    check_result = cmd.check_result
    check, result = check_result.check, check_result.result
    if result.status in (
        ResultStatus.ERROR,
        ResultStatus.WARNING,
    ):
        suppression_details = notification_suppression_details(check)
        if suppression_details:
            result.data = {
                **result.data,
                "notification_suppressed": suppression_details,
            }
    with uow:
        uow.store.results.add(result)
        uow.store.checks.add(check)
        uow.commit()

    # Persist every sample, but only trigger side effects when the failure streak
    # crosses the configured threshold.
    if _should_notify_check_result(
        check_result,
        uow,
        _notify_consecutive_failure_threshold(),
    ):
        notifier.notify_check_failed(check, result)


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
