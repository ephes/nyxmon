"""
CLI entrypoints for check management operations.
"""

import argparse
import sys
import anyio
import asyncio
import time
import aiosqlite
from pathlib import Path
from datetime import datetime

from nyxmon.adapters.repositories import SqliteStore
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckStatus
from nyxmon.domain.commands import AddCheck


# --- Add Check Functions ---


async def add_check_async(args):
    """Async function to add a check to the database."""
    # Validate database path
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Database file not found: {db_path}")
        sys.exit(1)

    try:
        # Initialize store and message bus
        store = SqliteStore(db_path=db_path)
        bus = bootstrap(store=store)

        # Get next check ID if not provided
        if args.check_id is None:
            # Get all checks and find the highest ID - use async method
            existing_checks = await bus.uow.store.checks.list_async()
            existing_checks = False
            if existing_checks:
                max_id = max(check.check_id for check in existing_checks)
                check_id = max_id + 1
            else:
                check_id = 1
        else:
            check_id = args.check_id

        # Create the check
        check = Check(
            check_id=check_id,
            service_id=args.service_id,
            check_type="http",
            status=CheckStatus.IDLE,
            url=args.url,
            check_interval=args.interval,
            data={},
        )

        # Add the check using the message bus
        cmd = AddCheck(check=check)
        bus.handle(cmd)

        print(f"✓ Successfully added check ID {check_id}")
        print(f"  Service ID: {args.service_id}")
        print(f"  Type: {args.check_type}")
        print(f"  URL: {args.url}")
        print(f"  Interval: {args.interval} seconds")

    except Exception as e:
        print(f"Error adding check: {e}")
        raise e


def add_check_to_db():
    """CLI script to add a health check to the database."""
    parser = argparse.ArgumentParser(
        description="Add a health check to NyxMon database"
    )
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument(
        "--service-id", type=int, required=True, help="Service ID for the check"
    )
    parser.add_argument(
        "--check-type",
        default="http",
        choices=["http", "tcp", "ping", "dns", "custom"],
        help="Type of health check (default: http)",
    )
    parser.add_argument("--url", required=True, help="URL or endpoint to check")
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Check interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--check-id",
        type=int,
        help="Specific check ID (will auto-increment if not provided)",
    )

    args = parser.parse_args()

    # Run the async function
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
