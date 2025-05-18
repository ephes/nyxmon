import sqlite3
import json
import logging
import time
from typing import List
import anyio
import aiosqlite

from pathlib import Path

from anyio.from_thread import BlockingPortalProvider

from ...domain import Check, Result, Service
from .interface import (
    RepositoryStore,
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
        data={},
    )
    return check


class SqliteCheckRepository(CheckRepository):
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
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
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_schema(db)
            db.row_factory = aiosqlite.Row
            [row] = await db.execute_fetchall(
                "SELECT id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled FROM health_check WHERE id = ?",
                (check_id,),
            )
            if row is None:
                raise KeyError(f"Check with ID {check_id} not found")
            return row_to_check(row)

    async def list_async(self) -> List[Check]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_schema(db)

            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled FROM health_check"
            )
            return [row_to_check(r) for r in rows]

    async def list_due_checks_async(self) -> List[Check]:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_schema(db)

            current_time = int(time.time())
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
                                LIMIT 100 -- Optional: process a batch at a time
                   )
                   RETURNING id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled""",
                (current_time, current_time),
            )

            rows = await result.fetchall()
            await db.commit()

            return [row_to_check(r) for r in rows]

    async def _add_async(self, check: Check) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_schema(db)

            await db.execute(
                """INSERT OR REPLACE INTO health_check
                   (id, service_id, name, check_type, url, check_interval, 
                    status, next_check_time, processing_started_at, disabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    int(check.disabled),  # Convert bool to int for SQLite
                ),
            )
            await db.commit()

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
                disabled         INTEGER DEFAULT 0
            );
            """
        )
        await db.commit()
        self._schema_ready = True


class SqliteResultRepository(ResultRepository):
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
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

    # ---------- interne async-Implementierung ----------
    async def _add_async(self, result: Result) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await self._ensure_schema(db)
            await db.execute(
                """INSERT INTO check_result (id, health_check_id, status, data, created_at)
                   VALUES (?, ?, ?, ?, datetime('now'))""",
                (
                    result.result_id,
                    result.check_id,
                    result.status,
                    json.dumps(result.data),
                ),
            )
            await db.commit()
            self.seen.add(result)

    async def _get_async(self, result_id: int) -> Result:
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
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
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
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
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
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
        async with aiosqlite.connect(self._db_path) as db:
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
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._connection: sqlite3.Connection | None = None
        self._thread_id: int | None = None

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

    @property
    def connection(self) -> sqlite3.Connection:
        # Create a new connection for the current thread if needed
        import threading

        thread_id = threading.get_ident()
        if self._thread_id != thread_id or self._connection is None:
            # Close existing connection if from a different thread
            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception:
                    pass

            # Create new connection for current thread
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            self._connection = conn
            self._thread_id = thread_id
            logger.debug(f"Created new SQLite connection for thread {thread_id}")

        # At this point self._connection should never be None
        assert self._connection is not None
        return self._connection

    def list(self) -> List:
        return [
            self.results,
            self.checks,
            self.services,
        ]
