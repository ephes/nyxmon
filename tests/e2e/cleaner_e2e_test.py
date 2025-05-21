import pytest
import os
import sqlite3
import json
from datetime import datetime, timedelta

import anyio
import aiosqlite

from nyxmon.bootstrap import bootstrap
from nyxmon.adapters.repositories import SqliteStore
from nyxmon.adapters.repositories.sqlite_repo import SqliteResultRepository
from nyxmon.adapters.cleaner import AsyncResultsCleaner, running_cleaner
from nyxmon.devdata import create_test_results


@pytest.fixture
def test_db_path(tmp_path):
    db_path = tmp_path / "test_cleaner.sqlite"
    yield db_path
    # Clean up
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def populated_db(test_db_path):
    """Create a test database with some results of different ages"""
    # Create the SQLite repository
    repo = SqliteResultRepository(db_path=test_db_path)

    # Generate test data using the devdata module
    test_results = create_test_results(num_recent=5, num_old=5, num_very_old=5)

    # Initialize the database and create schema by using the repository's schema creation method
    async def init_schema():
        async with aiosqlite.connect(test_db_path) as db:
            await repo._ensure_schema(db)

    # Run the async function to initialize the schema
    anyio.run(init_schema)

    # Now connect to populate with our test data
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()

    # Insert the results with appropriate timestamps
    now = datetime.now()
    recent_time = now - timedelta(minutes=10)
    old_time = now - timedelta(hours=25)
    very_old_time = now - timedelta(hours=48)

    # Add results with different timestamps
    for result in test_results["recent"]:
        cursor.execute(
            "INSERT INTO check_result (id, health_check_id, status, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                result.result_id,
                result.check_id,
                result.status,
                json.dumps(result.data),
                recent_time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    for result in test_results["old"]:
        cursor.execute(
            "INSERT INTO check_result (id, health_check_id, status, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                result.result_id,
                result.check_id,
                result.status,
                json.dumps(result.data),
                old_time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    for result in test_results["very_old"]:
        cursor.execute(
            "INSERT INTO check_result (id, health_check_id, status, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                result.result_id,
                result.check_id,
                result.status,
                json.dumps(result.data),
                very_old_time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

    conn.commit()
    conn.close()

    return test_db_path


@pytest.mark.anyio
async def test_cleaner_integration(populated_db):
    """Test that the cleaner correctly removes old results via command handlers"""
    # Set up the store with the populated database
    store = SqliteStore(db_path=populated_db)

    # Create a cleaner with a very short interval for testing
    cleaner = AsyncResultsCleaner(
        interval=0.5,  # Run every 0.5 seconds
        retention_period=24 * 60 * 60,  # 24 hours retention
        batch_size=1000,
    )

    # Bootstrap the system
    bus = bootstrap(store=store, cleaner=cleaner)

    # Verify initial state
    conn = sqlite3.connect(populated_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM check_result")
    initial_count = cursor.fetchone()[0]
    assert initial_count == 15  # Should have 15 total records
    conn.close()

    # Run the cleaner for a short time
    async with running_cleaner(bus):
        # Wait a bit for the cleaner to run
        await anyio.sleep(1.0)

    # Verify that old records were deleted
    conn = sqlite3.connect(populated_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM check_result")
    final_count = cursor.fetchone()[0]

    # Should have only kept the 5 recent records
    assert final_count == 5

    # Verify that only recent records remain
    cursor.execute("SELECT id FROM check_result ORDER BY id")
    remaining_ids = [row[0] for row in cursor.fetchall()]

    # Should only have IDs 1-5 (the recent records)
    assert set(remaining_ids) == set(range(1, 6))

    conn.close()


@pytest.mark.anyio
async def test_cleaner_with_batch_size(populated_db):
    """Test that the cleaner respects batch size"""
    # Set up the store with the populated database
    store = SqliteStore(db_path=populated_db)

    # Create a cleaner with a very short interval and small batch size
    cleaner = AsyncResultsCleaner(
        interval=0.5,  # Run every 0.5 seconds
        retention_period=24 * 60 * 60,  # 24 hours retention
        batch_size=2,  # Delete only 2 records per run
    )

    # Bootstrap the system
    bus = bootstrap(store=store, cleaner=cleaner)

    # Run the cleaner for a short time (enough for 1 run)
    async with running_cleaner(bus):
        # Wait a bit for the cleaner to run
        await anyio.sleep(0.7)

    # Verify that only a batch of old records was deleted
    conn = sqlite3.connect(populated_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM check_result")
    count_after_one_run = cursor.fetchone()[0]

    # Should have deleted 4 records (2 each from two runs) from the 10 old records
    assert count_after_one_run == 11

    conn.close()

    # Run again to delete more
    async with running_cleaner(bus):
        # Wait a bit for the cleaner to run
        await anyio.sleep(0.7)

    # Verify that another batch was deleted
    conn = sqlite3.connect(populated_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM check_result")
    count_after_two_runs = cursor.fetchone()[0]

    # Should have deleted more records, bringing total to 8 deleted
    assert count_after_two_runs == 7

    conn.close()
