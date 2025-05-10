#!/usr/bin/env python3
"""
Script to show all due checks from the NyxMon database.
"""

import argparse
import sys
import asyncio
import time
from pathlib import Path
from datetime import datetime

import aiosqlite


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

            if not rows:
                print("No checks found in the database.")
                return

            current_time = int(time.time())
            due_checks = []
            upcoming_checks = []
            processing_checks = []

            # Categorize checks
            for row in rows:
                if row["status"] == "processing":
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
            print(f"  Total checks: {len(rows)}")
            print(f"  Due now:      {len(due_checks)}")
            print(f"  Processing:   {len(processing_checks)}")
            print(f"  Upcoming:     {len(upcoming_checks)}")
            print("=" * 80)

    except Exception as e:
        print(f"Error reading database: {e}")
        sys.exit(1)


def main():
    """Main function for the show due checks script."""
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


if __name__ == "__main__":
    main()
