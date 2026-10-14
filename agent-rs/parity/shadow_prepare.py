#!/usr/bin/env python3
"""Prepare the database copy of a shadow run (PLAN.md 9.1 phase A, steps 1-2).

1. Copy the live database online with SQLite's backup API (the same as
   ``sqlite3 SRC ".backup DEST"``; consistent while the production agent and
   the dashboard keep writing). The destination must not exist yet.
2. In the copy only:
   - disable the ``smtp`` and ``imap`` checks: a shadow IMAP check would
     delete the messages the production IMAP check expects, a shadow SMTP
     check sends extra mail (phase C runs them once with ``run-check``);
   - release the claims inherited from the production process, which can
     never complete them in the copy (``status='idle'``,
     ``processing_started_at=0``, ``next_check_time`` kept), so they do not
     turn into shadow stale-lease results;
   - delete every ``collector_incident`` row, so shadow incidents start clean.
3. Print T0 (the copy time, UTC epoch and ``YYYY-MM-DD HH:MM:SS``) and write
   the copy's ``check_notification_state`` as the baseline B0 (JSON) next to
   the copy, for ``shadow_compare.py``.

Standard library only (runs with the host's ``python3`` on macmini). The
source database is opened read-only. Run it as the user that will run the
shadow agent (``nyxmon``) so the copy is owned by that user::

    sudo -u nyxmon python3 shadow_prepare.py \\
        --source /home/nyxmon/site/db.sqlite3 \\
        --dest /var/lib/nyxmon-shadow/db.sqlite3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
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

#: Check types that must not run from the copy (PLAN.md 9.1 step 2).
MAIL_TYPES = ("smtp", "imap")


def utc_text(epoch: int) -> str:
    """``datetime('now')`` format of SQLite: UTC ``YYYY-MM-DD HH:MM:SS``."""
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def backup(source: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        # Created with the umask of the caller; the shadow directory is 0700.
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def prepare(dest: Path) -> dict[str, Any]:
    """Apply step 2 to the copy; return what changed and the baseline."""
    connection = sqlite3.connect(dest, timeout=30)
    try:
        with connection:
            disabled = connection.execute(
                "UPDATE health_check SET disabled = 1 WHERE check_type IN (?, ?) AND disabled = 0",
                MAIL_TYPES,
            ).rowcount
            released = connection.execute(
                "UPDATE health_check SET status = 'idle', processing_started_at = 0"
                " WHERE status = 'processing'"
            ).rowcount
            incidents = connection.execute("DELETE FROM collector_incident").rowcount
        rows = connection.execute(
            f"SELECT {', '.join(STATE_COLUMNS)} FROM check_notification_state ORDER BY check_id"
        ).fetchall()
        enabled = connection.execute(
            "SELECT check_type, COUNT(*) FROM health_check WHERE disabled = 0"
            " GROUP BY check_type ORDER BY check_type"
        ).fetchall()
    finally:
        connection.close()
    return {
        "mail_checks_disabled": disabled,
        "claims_released": released,
        "incidents_deleted": incidents,
        "enabled_checks": dict(enabled),
        "baseline": [dict(zip(STATE_COLUMNS, row)) for row in rows],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", required=True, type=Path, help="live database (read-only)"
    )
    parser.add_argument(
        "--dest", required=True, type=Path, help="copy to create (must not exist)"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="where to write B0 (default: <dest>.baseline.json)",
    )
    args = parser.parse_args(argv)
    if not args.source.is_file():
        print(f"source database not found: {args.source}", file=sys.stderr)
        return 1
    if args.dest.exists() or Path(f"{args.dest}-journal").exists():
        print(f"refusing to overwrite {args.dest}", file=sys.stderr)
        return 1
    if args.source.resolve() == args.dest.resolve():
        print("source and destination are the same file", file=sys.stderr)
        return 1
    t0 = int(time.time())
    backup(args.source, args.dest)
    summary = prepare(args.dest)
    summary = {
        "t0": t0,
        "t0_utc": utc_text(t0),
        "source": str(args.source),
        "copy": str(args.dest),
        **summary,
    }
    baseline = args.baseline or Path(f"{args.dest}.baseline.json")
    fd = os.open(baseline, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(summary, out, indent=1, sort_keys=True)
    print(f"T0={t0} ({summary['t0_utc']} UTC)")
    print(
        f"copy {args.dest}: {summary['mail_checks_disabled']} mail check(s) disabled, "
        f"{summary['claims_released']} claim(s) released, "
        f"{summary['incidents_deleted']} incident row(s) deleted"
    )
    print(f"enabled checks by type: {summary['enabled_checks']}")
    print(
        f"baseline B0 ({len(summary['baseline'])} notification state rows): {baseline}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
