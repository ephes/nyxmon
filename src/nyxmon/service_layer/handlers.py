import asyncio
from typing import Callable, Awaitable, Any

from ..domain import events, commands
from .unit_of_work import UnitOfWork


def run_in_executor(func, *args, **kwargs) -> Awaitable[Any]:
    """Run a synchronous function in an executor to make it awaitable."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: func(*args, **kwargs))


def execute_checks(cmd: commands.ExecuteChecks, uow: UnitOfWork) -> None:
    with uow:
        checks = uow.store.checks.list()

        # Create coroutines for each check execution
        async def execute_all_checks():
            # Run all checks concurrently
            coroutines = [run_in_executor(check.execute) for check in checks]
            await asyncio.gather(*coroutines)

        # Run the coroutines in the event loop
        if checks:
            asyncio.run(execute_all_checks())

        # Add all results to the repository
        for check in checks:
            if check.result is not None:
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
