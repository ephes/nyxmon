import anyio
import argparse
import logging
import sys
import uvloop

from pathlib import Path

from nyxmon.adapters.collector import running_collector, AsyncCheckCollector
from nyxmon.adapters.cleaner import running_cleaner, AsyncResultsCleaner
from ..bootstrap import bootstrap
from ..adapters.repositories import SqliteStore
from ..adapters.notification import AsyncTelegramNotifier, LoggingNotifier
from ..startup_validation import validate_check_types

logger = logging.getLogger(__name__)


def setup_logging(log_level: str):
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def signal_handler(agent, signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    agent.stop()


async def run_monitoring_services(
    store,
    check_interval,
    cleanup_interval=3600,
    retention_period=86400,
    batch_size=1000,
    disable_cleaner=False,
    enable_telegram=False,
):
    """Async function to run the monitoring services (collector and cleaner)"""
    if enable_telegram:
        notifier = AsyncTelegramNotifier()
        logger.info("Telegram notifications enabled")
    else:
        notifier = LoggingNotifier()

    # Create components
    collector = AsyncCheckCollector(interval=check_interval)

    if not disable_cleaner:
        cleaner = AsyncResultsCleaner(
            interval=cleanup_interval,
            retention_period=retention_period,
            batch_size=batch_size,
        )
    else:
        cleaner = None

    # Bootstrap creates the runner internally, but we need access to it for validation
    # Create runner explicitly so we can validate check types
    from ..adapters.runner import AsyncCheckRunner
    from anyio.from_thread import BlockingPortalProvider

    portal_provider = BlockingPortalProvider()
    runner = AsyncCheckRunner(portal_provider=portal_provider)

    bus = bootstrap(
        store=store,
        collector=collector,
        cleaner=cleaner,
        notifier=notifier,
        runner=runner,
        portal_provider=portal_provider,
    )

    # Validate that all persisted checks have registered executors
    # This catches configuration issues early before attempting to run checks
    from ..service_layer import UnitOfWork

    uow = UnitOfWork(store=store)
    await validate_check_types(uow, runner)

    if disable_cleaner:
        # Only run the collector
        async with running_collector(bus):
            logger.info(f"Monitoring started with {check_interval}s check interval")
            logger.info("Results cleaner is disabled")

            # Wait forever until cancelled (e.g., by Ctrl+C)
            try:
                await anyio.sleep_forever()
            except BaseException:
                logger.info("Monitoring services shutting down...")
                raise
    else:
        # Run both collector and cleaner
        async with running_collector(bus), running_cleaner(bus):
            logger.info("Monitoring services started:")
            logger.info(f"- Check collector interval: {check_interval}s")
            logger.info(
                f"- Results cleaner interval: {cleanup_interval}s, retention: {retention_period}s"
            )

            # Wait forever until cancelled (e.g., by Ctrl+C)
            try:
                await anyio.sleep_forever()
            except BaseException:
                logger.info("Monitoring services shutting down...")
                raise


def start_agent():
    """CLI entrypoint for starting the monitoring agent."""
    parser = argparse.ArgumentParser(description="Run the NyxMon monitoring agent")
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument(
        "--interval", type=int, default=5, help="Check interval in seconds (default: 5)"
    )
    parser.add_argument(
        "--cleanup-interval",
        type=int,
        default=3600,
        help="Results cleanup interval in seconds (default: 3600 - 1 hour)",
    )
    parser.add_argument(
        "--retention-period",
        type=int,
        default=86400,
        help="Results retention period in seconds (default: 86400 - 24 hours)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Maximum number of old results to delete in a single batch (default: 1000)",
    )
    parser.add_argument(
        "--disable-cleaner",
        action="store_true",
        help="Disable the results cleaner",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)",
    )
    parser.add_argument(
        "--enable-telegram",
        action="store_true",
        help="Enable Telegram notifications (requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars)",
    )

    args = parser.parse_args()

    # Configure logging
    setup_logging(args.log_level)

    # Validate database path
    db_path = Path(args.db)
    if not db_path.exists():
        logger.error(f"Database file not found: {db_path}")
        sys.exit(1)

    try:
        logger.info(f"Initializing monitoring services with database: {args.db}")
        store = SqliteStore(db_path=db_path)

        async def main():
            await run_monitoring_services(
                store,
                args.interval,
                cleanup_interval=args.cleanup_interval,
                retention_period=args.retention_period,
                batch_size=args.batch_size,
                disable_cleaner=args.disable_cleaner,
                enable_telegram=args.enable_telegram,
            )

        # anyio automatically handles SIGINT and SIGTERM
        anyio.run(main, backend_options={"loop_factory": uvloop.new_event_loop})

    except KeyboardInterrupt:
        logger.info("Monitoring services stopped by user")
    except Exception:
        logger.exception("Error running monitoring services")
        sys.exit(1)


if __name__ == "__main__":
    start_agent()
