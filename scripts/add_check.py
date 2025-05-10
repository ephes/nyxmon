#!/usr/bin/env python3
"""
Script to add a health check to the NyxMon database.
"""

import argparse
import sys
import anyio
from pathlib import Path

from nyxmon.adapters.repositories import SqliteStore
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check
from nyxmon.domain.commands import AddCheck


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
            status="idle",
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
        # sys.exit(1)


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


if __name__ == "__main__":
    add_check_to_db()
