import inspect
import logging
from typing import Any, Callable

from anyio.from_thread import BlockingPortalProvider

from .adapters.runner import CheckRunner, AsyncCheckRunner
from .domain import Auto
from .adapters.collector import (
    CheckCollector,
    AsyncCheckCollector,
    site_incident_notifier,
)
from .adapters.cleaner import ResultsCleaner, AsyncResultsCleaner
from .adapters.repositories import RepositoryStore, InMemoryStore
from .adapters.notification import Notifier, LoggingNotifier
from .adapters.site_connectivity import (
    NullSiteState,
    ProbeRunner,
    SiteConnectivityConfig,
    SiteConnectivityObserver,
    SiteMode,
    SiteStateProvider,
)
from .service_layer import handlers, UnitOfWork, MessageBus

logger = logging.getLogger(__name__)


def inject_dependencies(handler, dependencies):
    params = inspect.signature(handler).parameters
    deps = {
        name: dependency for name, dependency in dependencies.items() if name in params
    }
    return lambda message: handler(message, **deps)


def _build_site_observer(
    config: SiteConnectivityConfig,
    *,
    store: RepositoryStore,
    notifier: Notifier,
    probe_runner: ProbeRunner | None,
    clock: Callable[[], float] | None,
) -> SiteConnectivityObserver:
    """Construct the observer with the collaborators it needs.

    Args:
        config: The resolved site connectivity configuration.
        store: The repository store; it is both the incident store (one
            permanent ``collector_incident`` row) and, through
            ``store.checks``, the scheduler the recovery recheck drives.
        notifier: Where the site ongoing alert and the recovery summary go.
        probe_runner: Injected probe implementation; ``None`` uses real TCP
            connects and ``getaddrinfo``.
        clock: Injected clock; ``None`` uses wall time.

    Returns:
        The observer, not yet started. The collector starts it.
    """
    return SiteConnectivityObserver(
        config,
        incident_store=store,  # type: ignore[arg-type]
        scheduler=store.checks,  # type: ignore[arg-type]
        probe_runner=probe_runner,
        clock=clock,
        notifier=site_incident_notifier(notifier.notify_check_failed),
    )


def bootstrap(
    uow: UnitOfWork = Auto,
    portal_provider: BlockingPortalProvider = Auto,
    store: RepositoryStore = Auto,
    collector: CheckCollector = Auto,
    cleaner: ResultsCleaner = Auto,
    runner: CheckRunner = Auto,
    notifier: Notifier = Auto,
    site_config: SiteConnectivityConfig | None = None,
    site_probe_runner: ProbeRunner | None = None,
    site_clock: Callable[[], float] | None = None,
) -> MessageBus:
    """Creates a new MessageBus instance with all dependencies injected.

    Args:
        uow: Unit of work; built over ``store`` when omitted.
        portal_provider: Blocking portal shared by every async adapter.
        store: Repository store; an in-memory one when omitted.
        collector: The check collector.
        cleaner: The results cleaner.
        runner: The check runner.
        notifier: Where alerts go.
        site_config: Site connectivity configuration; read from the
            environment when omitted. With ``mode = off`` no observer is
            created, no probe task runs, and nothing is ever held.
        site_probe_runner: Probe implementation for the observer, for tests.
        site_clock: Clock for the observer, for tests.

    Returns:
        The wired message bus.
    """
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

    if not cleaner:
        # Use default values for the cleaner (run every hour, keep results for 24 hours)
        cleaner = AsyncResultsCleaner()

    if not runner:
        runner = AsyncCheckRunner(portal_provider=portal_provider)

    if not notifier:
        # Use logging notifier by default
        notifier = LoggingNotifier()

    if hasattr(notifier, "set_portal_provider"):
        notifier.set_portal_provider(portal_provider)

    # Set store for cleaner
    cleaner.set_portal_provider(portal_provider)
    cleaner.set_store(store)

    if site_config is None:
        site_config = SiteConnectivityConfig.from_env()
    site_observer: SiteConnectivityObserver | None = None
    site_state: SiteStateProvider = NullSiteState()
    if site_config.mode is not SiteMode.OFF:
        site_observer = _build_site_observer(
            site_config,
            store=uow.store,
            notifier=notifier,
            probe_runner=site_probe_runner,
            clock=site_clock,
        )
        site_state = site_observer
        logger.info("site connectivity observation is %s", site_config.mode.value)

    dependencies: dict[str, Any] = {
        "uow": uow,
        "portal_provider": portal_provider,
        "collector": collector,
        "cleaner": cleaner,
        "runner": runner,
        "notifier": notifier,
        "site_state": site_state,
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
    recovery_store = uow.store.fork_for_concurrent_uow()
    recovery_dependencies = {
        **dependencies,
        "uow": UnitOfWork(store=recovery_store),
    }
    collector.set_recovery_handler(
        inject_dependencies(handlers.add_check_result, recovery_dependencies)
    )
    collector.set_process_notifier(notifier.notify_check_failed)
    if hasattr(collector, "set_incident_store"):
        # Collector-level incident dedup/reminder state must outlive the
        # process, so it lives in the store rather than in collector fields.
        collector.set_incident_store(uow.store)
    if site_observer is not None and hasattr(collector, "set_site_observer"):
        # The observer runs as a second task on the collector's portal and is
        # stopped with it.
        collector.set_site_observer(site_observer)
    return bus
