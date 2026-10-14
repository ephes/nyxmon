#!/usr/bin/env python3
"""Record the history of a shadow run (PLAN.md 9.1 phase A, step 4).

``check_notification_state`` keeps only the latest state and
``check_result`` only 24 hours, so this recorder opens the production and
the shadow database read-only (``mode=ro``) every ``--interval`` seconds and
appends one JSON line per database and tick to ``--out``:

- ``checks`` (first tick only): ``id -> [check_type, name, check_interval]``;
- ``results``: the new ``check_result`` rows (``[id, check_id, status,
  created_at, data]``); on the first tick only rows created at or after
  ``--since`` (UTC ``YYYY-MM-DD HH:MM:SS`` or a Unix epoch, normally T0);
- ``states`` / ``incidents``: every ``check_notification_state`` /
  ``collector_incident`` row that changed since the previous tick (all rows
  on the first tick), ``incidents_deleted`` for keys that disappeared;
- ``leases``: ``[id, status, processing_started_at, next_check_time,
  disabled]`` of every ``health_check`` row, so stuck claims are visible;
- ``retention``: ``count(*)``, ``min(created_at)``, ``min(id)`` of
  ``check_result`` (cleaner deletions);
- ``units``: for each ``--unit``, ``systemctl show`` properties (memory,
  CPU, main PID, restarts) when ``systemctl`` exists.

A database that cannot be read in a tick gets a line with ``error`` instead;
the next tick tries again. Stop with Ctrl-C or SIGTERM. Standard library
only (runs with the host's ``python3``)::

    python3 shadow_record.py --prod /home/nyxmon/site/db.sqlite3 \\
        --shadow /var/lib/nyxmon-shadow/db.sqlite3 --since <T0> \\
        --unit nyxmon-monitor --unit nyxmon-agent-shadow \\
        --out /var/lib/nyxmon-shadow/record.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

STATE_COLUMNS = (
    "check_id",
    "failure_count",
    "last_attempt_count",
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
    "held_since",
    "attempt_seq",
    "attempt_at",
)
UNIT_PROPERTIES = "MainPID,MemoryCurrent,MemoryPeak,CPUUsageNSec,NRestarts,ActiveState"


def since_text(value: str) -> str:
    """``--since`` as SQLite's ``created_at`` text (UTC)."""
    if value.isdigit():
        moment = dt.datetime.fromtimestamp(int(value), dt.timezone.utc)
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # validates the format
    return value


class DatabaseRecorder:
    """The incremental view of one database."""

    def __init__(self, name: str, path: Path, since: str) -> None:
        self.name = name
        self.path = path
        self.since = since
        self.last_result_id: int | None = None
        self.states: dict[int, list[Any]] = {}
        self.incidents: dict[str, list[Any]] = {}
        self.first = True

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)

    def tick(self, now: float) -> dict[str, Any]:
        # ``read_at``: when this database's snapshot began (``ts`` is the
        # tick, shared by both databases); journal lines are cut there.
        line: dict[str, Any] = {
            "ts": round(now, 3),
            "db": self.name,
            "read_at": round(time.time(), 3),
        }
        try:
            connection = self._connect()
        except sqlite3.Error as exc:
            line["error"] = f"{type(exc).__name__}: {exc}"
            return line
        try:
            # One read transaction: every section is the same snapshot.
            connection.execute("BEGIN")
            out, cursor = self._read(connection)
            connection.execute("COMMIT")
            # The cursors move only once the whole tick was read, so a
            # failed tick is recorded again in full by the next one.
            self.last_result_id = cursor["last_result_id"]
            self.states = cursor["states"]
            self.incidents = cursor["incidents"]
            self.first = False
            line.update(out)
        except sqlite3.Error as exc:
            line = {
                "ts": round(now, 3),
                "db": self.name,
                "read_at": line["read_at"],
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            connection.close()
        return line

    def _read(
        self, connection: sqlite3.Connection
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The tick's sections and the cursors to keep if it succeeds."""
        out: dict[str, Any] = {}
        if self.first:
            out["checks"] = {
                str(row[0]): [row[1], row[2], row[3]]
                for row in connection.execute(
                    "SELECT id, check_type, name, check_interval FROM health_check ORDER BY id"
                )
            }
        if self.last_result_id is None:
            rows = connection.execute(
                "SELECT id, health_check_id, status, created_at, data FROM check_result"
                " WHERE created_at >= ? ORDER BY id",
                (self.since,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, health_check_id, status, created_at, data FROM check_result"
                " WHERE id > ? ORDER BY id",
                (self.last_result_id,),
            ).fetchall()
        results = []
        for row in rows:
            try:
                data: Any = json.loads(row[4]) if row[4] else None
            except ValueError:
                data = row[4]
            results.append([row[0], row[1], row[2], row[3], data])
        out["results"] = results
        newest = connection.execute("SELECT MAX(id) FROM check_result").fetchone()[0]
        if self.last_result_id is None:
            last_result_id = max([newest or 0] + [row[0] for row in rows])
        else:
            last_result_id = rows[-1][0] if rows else self.last_result_id

        states = {
            row[0]: list(row)
            for row in connection.execute(
                f"SELECT {', '.join(STATE_COLUMNS)} FROM check_notification_state"
            )
        }
        out["states"] = [
            row for key, row in sorted(states.items()) if self.states.get(key) != row
        ]
        out["states_deleted"] = sorted(set(self.states) - set(states))

        incidents = {
            row[0]: list(row)
            for row in connection.execute(
                "SELECT incident_key, opened_at, last_alert_at, alert_count, payload FROM collector_incident"
            )
        }
        out["incidents"] = [
            row
            for key, row in sorted(incidents.items())
            if self.incidents.get(key) != row
        ]
        out["incidents_deleted"] = sorted(set(self.incidents) - set(incidents))

        out["leases"] = [
            list(row)
            for row in connection.execute(
                "SELECT id, status, processing_started_at, next_check_time, disabled"
                " FROM health_check ORDER BY id"
            )
        ]
        count, oldest, lowest = connection.execute(
            "SELECT COUNT(*), MIN(created_at), MIN(id) FROM check_result"
        ).fetchone()
        out["retention"] = {"count": count, "min_created_at": oldest, "min_id": lowest}
        cursor = {
            "last_result_id": last_result_id,
            "states": states,
            "incidents": incidents,
        }
        return out, cursor


def unit_properties(unit: str) -> dict[str, str]:
    if shutil.which("systemctl") is None:
        return {}
    completed = subprocess.run(
        ["systemctl", "show", "-p", UNIT_PROPERTIES, unit],
        capture_output=True,
        text=True,
        check=False,
    )
    properties = {}
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        properties[key] = value
    return properties


def record(
    recorders: list[DatabaseRecorder],
    units: list[str],
    out: Path,
    interval: float,
    stop: threading.Event,
    ticks: int | None = None,
) -> int:
    """Append ticks to ``out`` until ``stop`` is set (or ``ticks`` ran)."""
    done = 0
    with open(out, "a", encoding="utf-8") as stream:
        while not stop.is_set():
            now = time.time()
            for recorder in recorders:
                stream.write(
                    json.dumps(recorder.tick(now), separators=(",", ":")) + "\n"
                )
            if units:
                line = {
                    "ts": round(now, 3),
                    "units": {unit: unit_properties(unit) for unit in units},
                }
                stream.write(json.dumps(line, separators=(",", ":")) + "\n")
            stream.flush()
            done += 1
            if ticks is not None and done >= ticks:
                break
            stop.wait(max(0.0, interval - (time.time() - now)))
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prod", required=True, type=Path, help="production database")
    parser.add_argument(
        "--shadow", required=True, type=Path, help="shadow database copy"
    )
    parser.add_argument(
        "--since", required=True, help="T0: UTC 'YYYY-MM-DD HH:MM:SS' or Unix epoch"
    )
    parser.add_argument("--out", required=True, type=Path, help="JSONL file (appended)")
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between ticks"
    )
    parser.add_argument(
        "--unit", action="append", default=[], help="systemd unit to sample"
    )
    parser.add_argument("--ticks", type=int, help="stop after this many ticks (tests)")
    args = parser.parse_args(argv)
    try:
        since = since_text(args.since)
    except ValueError:
        parser.error("--since must be 'YYYY-MM-DD HH:MM:SS' (UTC) or a Unix epoch")
    for path in (args.prod, args.shadow):
        if not path.is_file():
            print(f"database not found: {path}", file=sys.stderr)
            return 1
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    recorders = [
        DatabaseRecorder("prod", args.prod, since),
        DatabaseRecorder("shadow", args.shadow, since),
    ]
    ticks = record(recorders, args.unit, args.out, args.interval, stop, args.ticks)
    print(f"recorded {ticks} tick(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
