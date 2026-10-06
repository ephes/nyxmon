"""
CLI entrypoints for check management operations.
"""

import argparse
import json
import sys
import anyio
import asyncio
import time
import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Sequence

from nyxmon.adapters.repositories import SqliteStore
from nyxmon.adapters.repositories.sqlite_repo import CheckIdExistsError
from nyxmon.domain import Check, CheckStatus, CheckType
from nyxmon.domain.dns_config import DnsCheckConfig
from nyxmon.domain.http_config import HttpCheckConfig
from nyxmon.domain.imap_config import ImapCheckConfig
from nyxmon.domain.json_metrics_config import JsonMetricsCheckConfig
from nyxmon.domain.ping_config import PingCheckConfig
from nyxmon.domain.smtp_config import SmtpCheckConfig
from nyxmon.domain.tcp_config import TcpCheckConfig


# --- Add Check Functions ---

# The config class each executor parses ``check.data`` with. ``add-check``
# validates ``--data`` through the same class, so a check it stores is one the
# agent can run.
CHECK_CONFIG_PARSERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    CheckType.HTTP: HttpCheckConfig.from_dict,
    CheckType.JSON_HTTP: HttpCheckConfig.from_dict,
    CheckType.TCP: TcpCheckConfig.from_dict,
    CheckType.PING: PingCheckConfig.from_dict,
    CheckType.DNS: DnsCheckConfig.from_dict,
    CheckType.SMTP: SmtpCheckConfig.from_dict,
    CheckType.IMAP: ImapCheckConfig.from_dict,
    CheckType.JSON_METRICS: JsonMetricsCheckConfig.from_dict,
}


def parse_check_data(raw: str | None, check_type: str) -> dict[str, Any]:
    """Parse and validate the ``--data`` argument for ``check_type``.

    Args:
        raw: The ``--data`` string, or ``None`` when it was not given.
        check_type: The check type the data configures.

    Returns:
        The decoded configuration object.

    Raises:
        ValueError: If ``raw`` is not a JSON object, or the check type's
            configuration rejects it.
    """
    if raw is None:
        data: Any = {}
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--data is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("--data must be a JSON object")

    parse_config = CHECK_CONFIG_PARSERS[check_type]
    try:
        config = parse_config(data)
        config.validate()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f"invalid --data for a {check_type} check: {exc}") from exc
    return data


async def add_check_async(args) -> int:
    """Store a new check and return its id."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database file not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    store = SqliteStore(db_path=db_path)
    check = Check(
        check_id=0,
        service_id=args.service_id,
        name=args.name,
        check_type=args.check_type,
        status=CheckStatus.IDLE,
        url=args.url,
        check_interval=args.interval,
        data=args.check_data,
    )
    try:
        check_id = await store.checks.create_async(
            check, check_id=args.check_id, replace=args.replace
        )
    except CheckIdExistsError as exc:
        print(
            f"Error: check ID {exc.check_id} already exists; "
            "pass --replace to overwrite it",
            file=sys.stderr,
        )
        sys.exit(1)

    verb = "saved" if args.replace else "added"
    print(f"✓ Successfully {verb} check ID {check_id}")
    if args.name:
        print(f"  Name: {args.name}")
    print(f"  Service ID: {args.service_id}")
    print(f"  Type: {args.check_type}")
    print(f"  URL: {args.url}")
    print(f"  Interval: {args.interval} seconds")
    return check_id


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return number


def build_add_check_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``add-check``."""
    parser = argparse.ArgumentParser(
        prog="add-check", description="Add a health check to NyxMon database"
    )
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument(
        "--service-id", type=int, required=True, help="Service ID for the check"
    )
    parser.add_argument("--name", default="", help="Display name of the check")
    parser.add_argument(
        "--check-type",
        default="http",
        choices=list(CHECK_CONFIG_PARSERS),
        help="Type of health check (default: http)",
    )
    parser.add_argument("--url", required=True, help="URL or endpoint to check")
    parser.add_argument(
        "--interval",
        type=_positive_int,
        default=300,
        help="Check interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--data",
        help=(
            "Check configuration as a JSON object (default: {}). It is "
            "validated against the check type's configuration."
        ),
    )
    parser.add_argument(
        "--check-id",
        type=_positive_int,
        help=(
            "Store the check under this ID (default: the next free ID). "
            "An existing ID is refused unless --replace is given."
        ),
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite the check given by --check-id if it already exists",
    )
    return parser


def add_check_to_db(argv: Sequence[str] | None = None) -> None:
    """CLI script to add a health check to the database."""
    parser = build_add_check_parser()
    args = parser.parse_args(argv)
    if args.replace and args.check_id is None:
        parser.error("--replace requires --check-id")
    try:
        args.check_data = parse_check_data(args.data, args.check_type)
    except ValueError as exc:
        parser.error(str(exc))

    anyio.run(add_check_async, args)


# --- Show Checks Functions ---


def format_time(timestamp):
    """Format Unix timestamp to human-readable string."""
    if timestamp == 0:
        return "Never"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def format_seconds_ago(timestamp):
    """Format Unix timestamp as 'X seconds/minutes/hours ago'."""
    if timestamp == 0:
        return "Never"

    current_time = time.time()
    diff = current_time - timestamp

    if diff < 60:
        return f"{int(diff)} seconds ago"
    elif diff < 3600:
        minutes = int(diff / 60)
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    elif diff < 86400:
        hours = int(diff / 3600)
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    else:
        days = int(diff / 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"


async def show_due_checks(db_path: Path):
    """Show all due checks from the database."""
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            # Get all checks with their service names
            query = """
            SELECT 
                hc.id as check_id,
                hc.service_id,
                hc.check_type,
                hc.url,
                hc.check_interval,
                hc.next_check_time,
                hc.processing_started_at,
                hc.status,
                s.name as service_name
            FROM health_check hc
            LEFT JOIN service s ON hc.service_id = s.id
            ORDER BY hc.next_check_time ASC
            """

            cursor = await db.execute(query)
            rows = await cursor.fetchall()
            rows_list = list(rows)  # Convert to list for type safety

            if not rows_list:
                print("No checks found in the database.")
                return

            current_time = int(time.time())
            due_checks = []
            upcoming_checks = []
            processing_checks = []

            # Categorize checks
            for row in rows_list:
                if row["status"] == CheckStatus.PROCESSING:
                    processing_checks.append(row)
                elif row["next_check_time"] <= current_time:
                    due_checks.append(row)
                else:
                    upcoming_checks.append(row)

            # Print results
            print("=" * 80)
            print(
                f"NyxMon Check Status Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            print("=" * 80)

            # Due checks
            if due_checks:
                print(f"\n📍 DUE CHECKS ({len(due_checks)}):")
                print("-" * 80)
                for check in due_checks:
                    print(f"  Check ID: {check['check_id']}")
                    print(
                        f"  Service:  {check['service_name']} (ID: {check['service_id']})"
                    )
                    print(f"  Type:     {check['check_type']}")
                    print(f"  URL:      {check['url']}")
                    print(f"  Due:      {format_seconds_ago(check['next_check_time'])}")
                    print(f"  Interval: {check['check_interval']} seconds")
                    print(f"  Status:   {check['status']}")
                    print("-" * 80)
            else:
                print("\n✅ No checks are currently due.")

            # Processing checks
            if processing_checks:
                print(f"\n⚡ PROCESSING CHECKS ({len(processing_checks)}):")
                print("-" * 80)
                for check in processing_checks:
                    print(f"  Check ID: {check['check_id']}")
                    print(
                        f"  Service:  {check['service_name']} (ID: {check['service_id']})"
                    )
                    print(f"  Type:     {check['check_type']}")
                    print(f"  URL:      {check['url']}")
                    print(
                        f"  Started:  {format_seconds_ago(check['processing_started_at'])}"
                    )
                    print(f"  Status:   {check['status']}")
                    print("-" * 80)

            # Upcoming checks
            if upcoming_checks:
                print("\n⏰ UPCOMING CHECKS (next 5):")
                print("-" * 80)
                for check in upcoming_checks[:5]:
                    time_until = check["next_check_time"] - current_time
                    if time_until < 60:
                        time_str = f"in {int(time_until)} seconds"
                    elif time_until < 3600:
                        time_str = f"in {int(time_until / 60)} minutes"
                    else:
                        time_str = f"in {int(time_until / 3600)} hours"

                    print(f"  Check ID: {check['check_id']}")
                    print(
                        f"  Service:  {check['service_name']} (ID: {check['service_id']})"
                    )
                    print(f"  Type:     {check['check_type']}")
                    print(f"  Next run: {time_str}")
                    print(f"  Status:   {check['status']}")
                    print("-" * 80)

            # Summary
            print("\nSUMMARY:")
            print(f"  Total checks: {len(rows_list)}")
            print(f"  Due now:      {len(due_checks)}")
            print(f"  Processing:   {len(processing_checks)}")
            print(f"  Upcoming:     {len(upcoming_checks)}")
            print("=" * 80)

    except Exception as e:
        print(f"Error reading database: {e}")
        sys.exit(1)


def show_checks():
    """CLI entrypoint for showing checks from the database."""
    parser = argparse.ArgumentParser(
        description="Show all due checks from NyxMon database"
    )
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show more detailed information"
    )

    args = parser.parse_args()

    # Validate database path
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database file not found: {db_path}")
        sys.exit(1)

    # Run the async function
    asyncio.run(show_due_checks(db_path))
