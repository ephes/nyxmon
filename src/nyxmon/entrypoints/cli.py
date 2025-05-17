import anyio
import argparse
import logging
import sys
import uvloop

from pathlib import Path

from nyxmon.adapters.collector import running_collector, AsyncCheckCollector
from ..bootstrap import bootstrap
from ..adapters.repositories import SqliteStore
from ..adapters.notification import AsyncTelegramNotifier, LoggingNotifier

logger = logging.getLogger(__name__)


def setup_logging(log_level: str):
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def signal_handler(agent, signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    agent.stop()


async def run_agent_with_collector(store, interval, enable_telegram=False):
    """Async function to run the agent with collector"""
    if enable_telegram:
        notifier = AsyncTelegramNotifier()
        logger.info("Telegram notifications enabled")
    else:
        notifier = LoggingNotifier()

    bus = bootstrap(
        store=store, collector=AsyncCheckCollector(interval=interval), notifier=notifier
    )

    async with running_collector(bus):
        logger.info(f"Agent started with {interval}s check interval")

        # Wait forever until cancelled (e.g., by Ctrl+C)
        try:
            await anyio.sleep_forever()
        except BaseException:
            logger.info("Agent shutting down...")
            raise


def start_agent():
    """CLI entrypoint for starting the monitoring agent."""
    parser = argparse.ArgumentParser(description="Run the NyxMon monitoring agent")
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument(
        "--interval", type=int, default=5, help="Check interval in seconds (default: 5)"
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
        logger.info(f"Initializing agent with database: {args.db}")
        store = SqliteStore(db_path=db_path)

        async def main():
            await run_agent_with_collector(
                store, args.interval, enable_telegram=args.enable_telegram
            )

        # anyio automatically handles SIGINT and SIGTERM
        anyio.run(main, backend_options={"loop_factory": uvloop.new_event_loop})

    except KeyboardInterrupt:
        logger.info("Agent stopped by user")
    except Exception:
        logger.exception("Error running agent")
        sys.exit(1)


if __name__ == "__main__":
    start_agent()
