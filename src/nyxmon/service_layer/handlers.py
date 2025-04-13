from typing import Callable

from ..domain import events, commands
from .unit_of_work import UnitOfWork


def execute_checks(cmd: commands.ExecuteChecks, uow: UnitOfWork) -> None:
    with uow:
        checks = uow.store.checks.list()
        for check in checks:
            check.execute()
            uow.store.results.add(check.result)
        uow.commit()


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
}
