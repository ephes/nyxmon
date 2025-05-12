from typing import Callable

from anyio.from_thread import BlockingPortalProvider

from ..adapters.collector import CheckCollector
from ..domain import events, commands
from ..adapters.runner import CheckRunner
from .unit_of_work import UnitOfWork
from ..domain.commands import AddResult


def execute_checks(
    cmd: commands.ExecuteChecks, runner: CheckRunner, uow: UnitOfWork
) -> None:
    """Execute all pending checks."""
    check_by_check_id = {check.check_id: check for check in cmd.checks}

    def result_received(result):
        check = check_by_check_id[result.check_id]
        inner_cmd = AddResult(check=check, result=result)
        check.add_result(result)  # raise events
        uow.store.checks.seen.add(
            check
        )  # mark as seen so that raised events will be collected
        uow.add_command(inner_cmd)  # add command to the unit of work

    runner.run_all(cmd.checks, result_received)


def add_check(cmd: commands.AddCheck, uow: UnitOfWork) -> None:
    """Add a check to the repository."""
    check = cmd.check
    with uow:
        uow.store.checks.add(check)
        uow.commit()


def add_result(cmd: commands.AddResult, uow: UnitOfWork) -> None:
    """Add a check to the repository."""
    result = cmd.result
    check = cmd.check
    with uow:
        uow.store.results.add(result)
        uow.store.checks.add(check)
        uow.commit()


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


def service_status_changed(event: events.ServiceStatusChanged, uow: UnitOfWork) -> None:
    with uow:
        service = uow.store.services.get(event.service_id)
        service.update_status(event.status)
        uow.commit()


EVENT_HANDLERS: dict[type[events.Event], list[Callable]] = {
    events.ServiceStatusChanged: [service_status_changed],
}

COMMAND_HANDLERS: dict[type[commands.Command], Callable] = {
    commands.ExecuteChecks: execute_checks,
    commands.AddCheck: add_check,
    commands.AddResult: add_result,
    commands.StartCollector: start_collector,
    commands.StopCollector: stop_collector,
}
