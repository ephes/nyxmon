import inspect

from anyio.from_thread import BlockingPortalProvider

from .adapters.runner import CheckRunner, AsyncCheckRunner
from .domain import Auto
from .adapters.collector import CheckCollector, AsyncCheckCollector
from .adapters.repositories import RepositoryStore, InMemoryStore
from .adapters.notification import Notifier, LoggingNotifier
from .service_layer import handlers, UnitOfWork, MessageBus


def inject_dependencies(handler, dependencies):
    params = inspect.signature(handler).parameters
    deps = {
        name: dependency for name, dependency in dependencies.items() if name in params
    }
    return lambda message: handler(message, **deps)


def bootstrap(
    uow: UnitOfWork = Auto,
    portal_provider: BlockingPortalProvider = Auto,
    store: RepositoryStore = Auto,
    collector: CheckCollector = Auto,
    runner: CheckRunner = Auto,
    notifier: Notifier = Auto,
) -> MessageBus:
    """Creates a new MessageBus instance with all dependencies injected."""
    if not store:
        store = InMemoryStore()

    if not uow:
        uow = UnitOfWork(store=store)

    if not portal_provider:
        portal_provider = BlockingPortalProvider()

    if hasattr(store, "set_portal_provider"):
        store.set_portal_provider(portal_provider)

    if not collector:
        collector = AsyncCheckCollector(interval=1)

    if not runner:
        runner = AsyncCheckRunner(portal_provider=portal_provider)

    if not notifier:
        # Use logging notifier by default
        notifier = LoggingNotifier()

    if hasattr(notifier, "set_portal_provider"):
        notifier.set_portal_provider(portal_provider)

    dependencies = {
        "uow": uow,
        "portal_provider": portal_provider,
        "collector": collector,
        "runner": runner,
        "notifier": notifier,
    }
    injected_event_handlers = {
        event_type: [
            inject_dependencies(handler, dependencies) for handler in event_handlers
        ]
        for event_type, event_handlers in handlers.EVENT_HANDLERS.items()
    }
    injected_command_handlers = {
        command_type: inject_dependencies(handler, dependencies)
        for command_type, handler in handlers.COMMAND_HANDLERS.items()
    }
    bus = MessageBus(
        uow=uow,
        event_handlers=injected_event_handlers,
        command_handlers=injected_command_handlers,
    )
    collector.set_message_bus(bus)
    return bus
