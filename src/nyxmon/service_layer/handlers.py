from typing import Callable

from anyio.from_thread import BlockingPortalProvider

from ..adapters.collector import CheckCollector
from ..adapters.notification import Notifier
from ..domain import events, commands
from ..domain.models import CheckResult
from ..adapters.runner import CheckRunner
from .unit_of_work import UnitOfWork
from ..domain.commands import AddCheckResult


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
    with uow:
        uow.store.results.add(result)
        uow.store.checks.add(check)
        uow.commit()

    # If the check_result should be notified, use the notifier to send notifications
    if check_result.should_notify:
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
}
