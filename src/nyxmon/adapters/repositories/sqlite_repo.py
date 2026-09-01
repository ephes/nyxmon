import sqlite3
import json
import logging
from time import time as current_epoch
import datetime
import threading
from typing import Any, List, cast
import anyio
import aiosqlite

from pathlib import Path

from anyio.from_thread import BlockingPortalProvider

from ...domain import Check, Result, Service
from .interface import (
    RepositoryStore,
    NotificationStateConflict,
    check_batch_size,
    CheckRepository,
    ResultRepository,
    ServiceRepository,
)

logger = logging.getLogger(__name__)


def row_to_check(row: aiosqlite.Row) -> Check:
    check_id = row["id"]
    service_id = row["service_id"]
    name = row["name"]
    check_type = row["check_type"]
    url = row["url"]
    check_interval = row["check_interval"]
    next_check_time = row["next_check_time"]
    processing_started_at = row["processing_started_at"]
    status = row["status"]
    disabled = bool(row["disabled"])  # SQLite stores booleans as 0/1

    # Parse JSON data column (handle missing column gracefully for migration)
    # Note: aiosqlite.Row doesn't support .get(), use KeyError guard
    try:
        data_raw = row["data"]
    except (KeyError, IndexError):
        # Column doesn't exist yet (migration in progress)
        data: dict[str, Any] = {}
    else:
        if data_raw is None:
            data = {}
        elif isinstance(data_raw, str):
            data = json.loads(data_raw) if data_raw else {}
        elif isinstance(data_raw, bytes):
            data = json.loads(data_raw.decode("utf-8")) if data_raw else {}
        elif isinstance(data_raw, dict):
            data = data_raw
        else:
            data = cast(dict[str, Any], data_raw)

    check = Check(
        check_id=check_id,
        service_id=service_id,
        name=name,
        check_type=check_type,
        url=url,
        check_interval=check_interval,
        next_check_time=next_check_time,
        processing_started_at=processing_started_at,
        status=status,
        disabled=disabled,
        data=data,
    )
    return check


class SqliteCheckRepository(CheckRepository):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._use_uri = self._db_path.startswith("file:")
        self._portal_provider: BlockingPortalProvider | None = None
        self._schema_ready = False
        self.seen: set[Check] = set()

    # ---------- öffentliche, SYNCHRONE Ports ----------
    def get(self, check_id: int) -> Check:
        """Get a check from the repository by ID."""
        return self._await(self._get_async(check_id))

    def list(self) -> List[Check]:
        return self._await(self.list_async())

    def add(self, check: Check) -> None:
        if self._portal_provider is None:
            return  # No portal provider set, cannot add check
        with self._portal_provider as portal:
            portal.call(self._add_async, check)

    # ---------- interne async-Implementierung ----------
    async def _get_async(self, check_id: int) -> Check:
        """Get a check from the repository by ID asynchronously."""
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            [row] = await db.execute_fetchall(
                "SELECT id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data FROM health_check WHERE id = ?",
                (check_id,),
            )
            if row is None:
                raise KeyError(f"Check with ID {check_id} not found")
            return row_to_check(row)

    async def list_async(self) -> List[Check]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data FROM health_check"
            )
            return [row_to_check(r) for r in rows]

    async def list_due_checks_async(self) -> List[Check]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            current_time = int(current_epoch())
            db.row_factory = aiosqlite.Row

            # Single atomic operation to find and claim checks
            # Using SQLite's RETURNING clause (available in SQLite 3.35.0+)
            result = await db.execute(
                """UPDATE health_check
                   SET status                = 'processing',
                       processing_started_at = ?
                   WHERE id IN (SELECT id
                                FROM health_check
                                WHERE next_check_time <= ?
                                  AND status = 'idle'
                                  AND disabled = 0
                                ORDER BY next_check_time ASC, id ASC
                                LIMIT ?
                   )
                   RETURNING id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data""",
                (current_time, current_time, check_batch_size()),
            )

            rows = await result.fetchall()
            await db.commit()

            return [row_to_check(r) for r in rows]

    async def reclaim_stale_checks_async(self, lease_seconds: int) -> List[Check]:
        """Atomically release checks abandoned by an interrupted worker."""
        current_time = int(current_epoch())
        stale_before = current_time - lease_seconds
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            candidate = await db.execute_fetchall(
                """SELECT 1 FROM health_check
                   WHERE status = 'processing'
                     AND (COALESCE(processing_started_at, 0) = 0
                          OR processing_started_at <= ?)
                   LIMIT 1""",
                (stale_before,),
            )
            if not candidate:
                return []
            # Legacy or malformed claims without a timestamp receive a full lease
            # from first observation instead of being reclaimed immediately.
            await db.execute(
                """UPDATE health_check
                   SET processing_started_at = ?
                   WHERE status = 'processing'
                     AND COALESCE(processing_started_at, 0) = 0""",
                (current_time,),
            )
            result = await db.execute(
                """UPDATE health_check
                   SET status = 'idle', processing_started_at = 0
                   WHERE id IN (
                       SELECT id FROM health_check
                       WHERE status = 'processing'
                         AND processing_started_at > 0
                         AND processing_started_at <= ?
                       ORDER BY processing_started_at ASC, id ASC
                       LIMIT ?
                   )
                   RETURNING id, service_id, name, check_type, url, check_interval,
                             next_check_time, processing_started_at, status, disabled, data""",
                (stale_before, check_batch_size()),
            )
            rows = await result.fetchall()
            await db.commit()
            return [row_to_check(row) for row in rows]

    def get_notification_state(self, check_id: int) -> tuple[int, int, int]:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for notification state")
        with self._portal_provider as portal:
            return portal.call(self._get_notification_state_async, check_id)

    async def _get_notification_state_async(
        self, check_id: int
    ) -> tuple[int, int, int]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            cursor = await db.execute(
                """SELECT failure_count, last_attempt_count, last_immediate_at
                   FROM check_notification_state WHERE check_id = ?""",
                (check_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return (0, 0, 0)
            return (int(row[0]), int(row[1]), int(row[2]))

    def set_notification_state(
        self,
        check_id: int,
        failure_count: int,
        last_attempt_count: int,
        last_immediate_at: int,
    ) -> None:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required for notification state")
        with self._portal_provider as portal:
            portal.call(
                self._set_notification_state_async,
                check_id,
                failure_count,
                last_attempt_count,
                last_immediate_at,
            )

    async def _set_notification_state_async(
        self,
        check_id: int,
        failure_count: int,
        last_attempt_count: int,
        last_immediate_at: int,
    ) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            await db.execute(
                """INSERT INTO check_notification_state
                       (check_id, failure_count, last_attempt_count, last_immediate_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(check_id) DO UPDATE SET
                       failure_count = excluded.failure_count,
                       last_attempt_count = excluded.last_attempt_count,
                       last_immediate_at = excluded.last_immediate_at""",
                (check_id, failure_count, last_attempt_count, last_immediate_at),
            )
            await db.commit()

    async def _add_async(self, check: Check) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            await self._upsert_on_connection(db, check)
            await db.commit()

    @staticmethod
    async def _upsert_on_connection(db: aiosqlite.Connection, check: Check) -> None:
        await db.execute(
            """INSERT INTO health_check
               (id, service_id, name, check_type, url, check_interval,
                status, next_check_time, processing_started_at, disabled, data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   service_id = excluded.service_id,
                   name = excluded.name,
                   check_type = excluded.check_type,
                   url = excluded.url,
                   check_interval = excluded.check_interval,
                   status = excluded.status,
                   next_check_time = excluded.next_check_time,
                   processing_started_at = excluded.processing_started_at,
                   disabled = excluded.disabled,
                   data = excluded.data""",
            (
                check.check_id,
                check.service_id,
                check.name,
                check.check_type,
                check.url,
                check.check_interval,
                check.status,
                check.next_check_time,
                check.processing_started_at,
                int(check.disabled),
                json.dumps(check.data),
            ),
        )

    @staticmethod
    async def _complete_on_connection(db: aiosqlite.Connection, check: Check) -> bool:
        cursor = await db.execute(
            """UPDATE health_check
               SET status = ?, next_check_time = ?, processing_started_at = ?
               WHERE id = ?
                 AND ((status = 'processing' AND processing_started_at = ?)
                      OR (status <> 'processing' AND ? = 0))""",
            (
                check.status,
                check.next_check_time,
                check.processing_started_at,
                check.check_id,
                check.claim_started_at,
                check.claim_started_at,
            ),
        )
        return bool(cursor.rowcount)

    # ---------- Bridge sync → async ----------
    def _await(self, coro):
        async def _run():
            return await coro  # Coroutine tatsächlich ausführen

        return anyio.from_thread.run(_run)  # Callable (!) an from_thread.run()

    # ---------- einmalige Schema-Initialisierung ----------

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return

        # Create table if not exists
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS health_check (
                id               INTEGER PRIMARY KEY,
                service_id       INTEGER NOT NULL,
                name             TEXT    DEFAULT '',
                check_type       TEXT    NOT NULL,
                url              TEXT    NOT NULL,
                check_interval   INTEGER NOT NULL,
                status           TEXT    DEFAULT 'idle',
                next_check_time  INTEGER DEFAULT 0,
                processing_started_at INTEGER DEFAULT 0,
                disabled         INTEGER DEFAULT 0,
                data             TEXT    DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS check_notification_state (
                check_id           INTEGER PRIMARY KEY
                                   REFERENCES health_check(id) ON DELETE CASCADE,
                failure_count      INTEGER NOT NULL DEFAULT 0,
                last_attempt_count INTEGER NOT NULL DEFAULT 0,
                last_immediate_at  INTEGER NOT NULL DEFAULT 0
            );
            """
        )

        # Add data column to existing tables (idempotent migration)
        # This handles databases created before data column was added
        try:
            await db.execute(
                """ALTER TABLE health_check ADD COLUMN data TEXT DEFAULT '{}'"""
            )
        except sqlite3.OperationalError as e:
            # Column already exists, ignore
            if "duplicate column name" not in str(e).lower():
                raise

        await db.commit()
        self._schema_ready = True


class SqliteResultRepository(ResultRepository):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._use_uri = self._db_path.startswith("file:")
        self._schema_ready = False
        self.seen = set()
        self._portal_provider: BlockingPortalProvider | None = None

    # ---------- öffentliche, SYNCHRONE Ports ----------
    def add(self, result: Result) -> None:
        if self._portal_provider is None:
            return  # No portal provider set, cannot add a result
        with self._portal_provider as portal:
            portal.call(self._add_async, result)

    def get(self, result_id: int) -> Result:
        return self._await(self._get_async(result_id))

    def list(self) -> List[Result]:
        return self._await(self._list_async())

    def list_for_check(self, check_id: int, limit: int) -> List[Result]:
        return self._await(self._list_for_check_async(check_id, limit))

    # ---------- interne async-Implementierung ----------
    async def _add_async(self, result: Result) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            await self._insert_on_connection(db, result)
            await db.commit()
            self.seen.add(result)

    @staticmethod
    async def _insert_on_connection(db: aiosqlite.Connection, result: Result) -> None:
        await db.execute(
            """INSERT INTO check_result
                   (id, health_check_id, status, data, created_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (
                result.result_id,
                result.check_id,
                result.status,
                json.dumps(result.data),
            ),
        )

    async def _get_async(self, result_id: int) -> Result:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            [row] = await db.execute_fetchall(
                "SELECT id, health_check_id, status, data FROM check_result WHERE id = ?",
                (result_id,),
            )
            if row is None:
                raise KeyError(f"Result with ID {result_id} not found")
            return Result(
                result_id=row["id"],
                check_id=row["check_id"],
                status=row["status"],
                data=json.loads(row["data"]),
            )

    async def _list_async(self) -> List[Result]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, health_check_id, status, data FROM check_result"
            )
            return [
                Result(
                    result_id=row["id"],
                    check_id=row["health_check_id"],
                    status=row["status"],
                    data=json.loads(row["data"]),
                )
                for row in rows
            ]

    async def _list_for_check_async(self, check_id: int, limit: int) -> List[Result]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT id, health_check_id, status, data
                FROM check_result
                WHERE health_check_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (check_id, limit),
            )
            return [
                Result(
                    result_id=row["id"],
                    check_id=row["health_check_id"],
                    status=row["status"],
                    data=json.loads(row["data"] or "{}"),
                )
                for row in rows
            ]

    async def delete_old_results_async(
        self, retention_seconds: int = 86400, batch_size: int = 1000
    ) -> int:
        """Delete check results older than the specified period."""
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            # Calculate the cutoff timestamp (SQLite timestamp format)
            cutoff_time = datetime.datetime.now() - datetime.timedelta(
                seconds=retention_seconds
            )
            cutoff_time_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")

            # First, get the IDs to delete (with limit)
            # SQLite doesn't support LIMIT in DELETE directly, so we need to do this in two steps
            cursor = await db.execute(
                "SELECT id FROM check_result WHERE created_at < ? ORDER BY id LIMIT ?",
                (cutoff_time_str, batch_size),
            )
            rows = await cursor.fetchall()

            if not rows:
                return 0

            # Get the IDs to delete as a tuple
            ids_to_delete = tuple(row[0] for row in rows)

            # If only one ID to delete, we need special SQL syntax (can't use 'IN' with a single value tuple)
            if len(ids_to_delete) == 1:
                delete_sql = "DELETE FROM check_result WHERE id = ?"
                params = (ids_to_delete[0],)
            else:
                placeholders = ",".join(["?"] * len(ids_to_delete))
                delete_sql = f"DELETE FROM check_result WHERE id IN ({placeholders})"
                params = ids_to_delete

            # Delete the records
            cursor = await db.execute(delete_sql, params)
            deleted_count = cursor.rowcount
            await db.commit()

            return deleted_count

    def delete_old_results(
        self, retention_seconds: int = 86400, batch_size: int = 1000
    ) -> int:
        """Delete check results older than the specified period."""
        return self._await(
            self.delete_old_results_async(
                retention_seconds=retention_seconds, batch_size=batch_size
            )
        )

    # ---------- Bridge sync → async ----------
    def _await(self, coro):
        async def _run():
            return await coro

        return anyio.from_thread.run(_run)

    # ---------- einmalige Schema-Initialisierung ----------
    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS check_result (
                id              INTEGER PRIMARY KEY,
                health_check_id INTEGER NOT NULL,
                status          TEXT NOT NULL,
                data            TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await db.commit()
        self._schema_ready = True


class SqliteServiceRepository(ServiceRepository):
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = db_path
        self._use_uri = str(db_path).startswith("file:")
        self._portal_provider: BlockingPortalProvider | None = None
        self._schema_ready = False
        self.seen: set[Service] = set()

    # ---------- öffentliche, SYNCHRONE Ports ----------
    def list(self) -> List[Service]:
        return self._await(self.list_async())

    def add(self, service: Service) -> None:
        if self._portal_provider is None:
            return  # No portal provider set, cannot add service
        with self._portal_provider as portal:
            portal.call(self._add_async, service)

    def get(self, service_id: int) -> Service:
        return self._await(self._get_async(service_id))

    # ---------- interne async-Implementierung ----------
    async def list_async(self) -> List[Service]:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall("SELECT id, name FROM service")
            services = []
            for row in rows:
                service_id, name = row
                data = {"name": name}
                services.append(Service(service_id=service_id, data=data))
            return services

    async def _add_async(self, service: Service) -> None:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            await db.execute(
                """INSERT OR REPLACE INTO service (id, name) VALUES (?, ?)""",
                (
                    service.service_id,
                    service.data.get("name", ""),
                ),
            )
            await db.commit()
            self.seen.add(service)

    async def _get_async(self, service_id: int) -> Service:
        async with aiosqlite.connect(self._db_path, uri=self._use_uri) as db:
            await self._ensure_schema(db)

            db.row_factory = aiosqlite.Row
            [row] = await db.execute_fetchall(
                "SELECT id, name FROM service WHERE id = ?", (service_id,)
            )
            if row is None:
                raise KeyError(f"Service with ID {service_id} not found")

            service_id, name = row
            data = {"name": name}
            return Service(service_id=service_id, data=data)

    # ---------- Bridge sync → async ----------
    def _await(self, coro):
        async def _run():
            return await coro  # Coroutine tatsächlich ausführen

        return anyio.from_thread.run(_run)  # Callable (!) an from_thread.run()

    # ---------- einmalige Schema-Initialisierung ----------
    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        if self._schema_ready:
            return
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS service
            (
                id   INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );
            """
        )
        await db.commit()
        self._schema_ready = True


class SqliteStore(RepositoryStore):
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = db_path
        self._use_uri = str(db_path).startswith("file:")
        self._connection_state = threading.local()

        # Initialize repositories
        self.results = SqliteResultRepository(db_path)
        self.checks = SqliteCheckRepository(db_path)
        self.services = SqliteServiceRepository(db_path)

        # Blocking portal provider
        self._portal_provider: BlockingPortalProvider | None = None

    def set_portal_provider(self, portal_provider: BlockingPortalProvider) -> None:
        """Set the portal provider for the store."""
        self._portal_provider = portal_provider
        self.results._portal_provider = portal_provider
        self.checks._portal_provider = portal_provider
        self.services._portal_provider = portal_provider

    def fork_for_concurrent_uow(self) -> "SqliteStore":
        """Use independent repositories and event sets over the same database."""
        store = SqliteStore(self.db_path)
        if self._portal_provider is not None:
            store.set_portal_provider(self._portal_provider)
        return store

    def persist_check_result(
        self,
        check: Check,
        result: Result,
        notification_transition: (
            tuple[tuple[int, int, int], tuple[int, int, int]] | None
        ),
        *,
        complete_check: bool = True,
    ) -> bool:
        if self._portal_provider is None:
            raise RuntimeError("portal provider is required to persist check results")
        with self._portal_provider as portal:
            return portal.call(
                self._persist_check_result_async,
                check,
                result,
                notification_transition,
                complete_check,
            )

    async def _persist_check_result_async(
        self,
        check: Check,
        result: Result,
        notification_transition: (
            tuple[tuple[int, int, int], tuple[int, int, int]] | None
        ),
        complete_check: bool = True,
    ) -> bool:
        async with aiosqlite.connect(self.db_path, uri=self._use_uri) as db:
            await self.checks._ensure_schema(db)
            await self.results._ensure_schema(db)
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            completion_applied = True
            if complete_check:
                completion_applied = await self.checks._complete_on_connection(
                    db, check
                )
            if not complete_check or not completion_applied:
                exists = await db.execute(
                    "SELECT 1 FROM health_check WHERE id = ?", (check.check_id,)
                )
                if await exists.fetchone() is None:
                    await db.rollback()
                    logger.info(
                        "dropped late result for deleted check_id=%s",
                        check.check_id,
                    )
                    return False
                if complete_check:
                    logger.info(
                        "stored late result without changing newer claim for check_id=%s",
                        check.check_id,
                    )
            await self.results._insert_on_connection(db, result)
            if complete_check and not completion_applied:
                await db.commit()
                self.results.seen.add(result)
                return False
            if notification_transition is not None:
                expected_state, notification_state = notification_transition
                cursor = await db.execute(
                    """SELECT failure_count, last_attempt_count, last_immediate_at
                       FROM check_notification_state WHERE check_id = ?""",
                    (check.check_id,),
                )
                row = await cursor.fetchone()
                current_state = (
                    (int(row[0]), int(row[1]), int(row[2]))
                    if row is not None
                    else (0, 0, 0)
                )
                if current_state != expected_state:
                    await db.rollback()
                    raise NotificationStateConflict(check.check_id)
            if notification_transition is not None:
                expected_state, notification_state = notification_transition
                if notification_state != expected_state:
                    await db.execute(
                        """INSERT INTO check_notification_state
                               (check_id, failure_count, last_attempt_count,
                                last_immediate_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(check_id) DO UPDATE SET
                               failure_count = excluded.failure_count,
                               last_attempt_count = excluded.last_attempt_count,
                               last_immediate_at = excluded.last_immediate_at""",
                        (check.check_id, *notification_state),
                    )
            await db.commit()
            if complete_check:
                self.checks.seen.add(check)
            self.results.seen.add(result)
            return True

    @property
    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._connection_state, "connection", None)
        if connection is None:
            conn = sqlite3.connect(self.db_path, uri=self._use_uri)
            conn.row_factory = sqlite3.Row
            self._connection_state.connection = conn
            connection = conn
            logger.debug(
                "Created new SQLite connection for thread %s",
                threading.get_ident(),
            )
        return connection

    def list(self) -> List:
        return [
            self.results,
            self.checks,
            self.services,
        ]
