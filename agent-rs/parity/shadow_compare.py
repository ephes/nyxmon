#!/usr/bin/env python3
"""Compare a shadow run with production (PLAN.md 9.1 phase A, steps 4-5).

Reads the JSONL written by ``shadow_record.py`` and reports, per enabled
check that is not a mail check (``smtp``/``imap`` are disabled in the copy):

- statuses and error types over the window: counts per database, and every
  shadow sample whose ``(status, error_type)`` differs from the production
  sample nearest in time, with both samples' timestamps, so each
  disagreement can be explained (the endpoint changed between the samples)
  or not;
- data key lists per ``(status, error_type)`` that occur in only one
  database (key order included);
- alerts: ``attempt_seq(end) - attempt_seq(T0)`` per database (the exact
  number of notification decisions, PLAN.md 9.1; ``attempt_seq(T0)`` from
  the baseline B0 of ``shadow_prepare.py`` with ``--baseline``, else the
  first recorded tick) and the ticks at which it rose; with ``--shadow-log``
  also the ``notification check_id=...`` lines of the shadow agent's
  journal;
- ``stale_processing_lease`` results (none expected in the shadow database);
- claims stuck in ``processing`` longer than ``--lease-seconds`` in any
  recorded lease snapshot;
- cleaner deletions of the shadow database: once ``--retention-seconds``
  plus ``--cleanup-interval`` have passed since T0, ``min(created_at)`` must
  stay within that age and ``min(id)`` must advance;
- memory and CPU of the sampled systemd units (PLAN.md 4.4 budgets);
- with ``--prod-db``/``--shadow-db``, the same lease and retention queries
  once more directly against both databases (read-only), as a cross-check.

The report ends with a list of findings to explain; ``--json PATH`` also
writes everything as JSON. Exit status 0 (1 for unreadable input, or with
``--fail-on-findings`` when findings remain). Standard library only::

    python3 shadow_compare.py --record record.jsonl --since <T0> \\
        --baseline /var/lib/nyxmon-shadow/db.sqlite3.baseline.json \\
        --shadow-log shadow-journal.txt \\
        --prod-db /home/nyxmon/site/db.sqlite3 \\
        --shadow-db /var/lib/nyxmon-shadow/db.sqlite3
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DATABASES = ("prod", "shadow")
MAIL_TYPES = {"smtp", "imap"}
#: Index of ``attempt_seq`` in a recorded state row (``shadow_record.py``).
ATTEMPT_SEQ = 7
SHADOW_ALERT = re.compile(r"notification check_id=(\d+) status=(\w+) error_type=(\S*)")
#: PLAN.md 4.4: steady-state RSS and peak budgets of the Rust agent.
RSS_BUDGET = 20 * 1024 * 1024
PEAK_BUDGET = 40 * 1024 * 1024


def epoch(value: str | int | float) -> float:
    """Unix epoch of a recorded timestamp or SQLite ``created_at`` text."""
    if isinstance(value, (int, float)):
        return float(value)
    if value.isdigit():
        return float(value)
    moment = dt.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    return moment.replace(tzinfo=dt.timezone.utc).timestamp()


def utc(value: float) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


@dataclass
class Sample:
    result_id: int
    created_at: float
    status: str
    error_type: str
    keys: tuple[str, ...]


@dataclass
class Database:
    name: str
    checks: dict[int, list[Any]] = field(default_factory=dict)
    samples: dict[int, list[Sample]] = field(default_factory=lambda: defaultdict(list))
    seen: set[int] = field(default_factory=set)
    #: ``check_id -> [(ts, attempt_seq)]`` for every recorded change.
    attempts: dict[int, list[tuple[float, int]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    incidents: dict[str, list[Any]] = field(default_factory=dict)
    leases: list[tuple[float, list[list[Any]]]] = field(default_factory=list)
    retention: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    errors: list[tuple[float, str]] = field(default_factory=list)
    #: The tick that recorded every state row (the recorder's first).
    baseline_ts: float | None = None

    def add(self, line: dict[str, Any]) -> None:
        ts = float(line["ts"])
        if "error" in line:
            self.errors.append((ts, str(line["error"])))
            return
        if self.baseline_ts is None:
            self.baseline_ts = ts
        for key, value in line.get("checks", {}).items():
            self.checks[int(key)] = value
        for result_id, check_id, status, created_at, data in line.get("results", []):
            if result_id in self.seen:
                continue
            self.seen.add(result_id)
            keys = tuple(data) if isinstance(data, dict) else ()
            error_type = (
                str(data.get("error_type", "")) if isinstance(data, dict) else ""
            )
            self.samples[check_id].append(
                Sample(result_id, epoch(created_at), status, error_type, keys)
            )
        for row in line.get("states", []):
            self.attempts[row[0]].append((ts, int(row[ATTEMPT_SEQ])))
        for row in line.get("incidents", []):
            self.incidents[row[0]] = row
        for key in line.get("incidents_deleted", []):
            self.incidents.pop(key, None)
        if "leases" in line:
            self.leases.append((ts, line["leases"]))
        if "retention" in line:
            self.retention.append((ts, line["retention"]))


def load(
    record: Path, since: float
) -> tuple[dict[str, Database], dict[str, list[tuple[float, dict[str, str]]]]]:
    databases = {name: Database(name) for name in DATABASES}
    units: dict[str, list[tuple[float, dict[str, str]]]] = defaultdict(list)
    with open(record, encoding="utf-8") as stream:
        for number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                line = json.loads(raw)
            except ValueError as exc:
                raise SystemExit(f"{record}:{number}: not JSON ({exc})") from None
            if "units" in line:
                for unit, properties in line["units"].items():
                    units[unit].append((float(line["ts"]), properties))
            elif line.get("db") in databases:
                databases[line["db"]].add(line)
    for database in databases.values():
        for check_id, samples in database.samples.items():
            samples[:] = sorted(
                (sample for sample in samples if sample.created_at >= since),
                key=lambda sample: (sample.created_at, sample.result_id),
            )
    return databases, units


def compared_checks(prod: Database, shadow: Database) -> list[int]:
    """Checks enabled in both databases at the end, mail checks excluded."""
    if not prod.leases or not shadow.leases:
        return []
    enabled = {
        name: {row[0] for row in database.leases[-1][1] if not row[4]}
        for name, database in (("prod", prod), ("shadow", shadow))
    }
    return sorted(
        check_id
        for check_id in enabled["prod"] & enabled["shadow"]
        if str(prod.checks.get(check_id, [""])[0]) not in MAIL_TYPES
    )


def nearest(samples: list[Sample], moment: float) -> Sample | None:
    if not samples:
        return None
    times = [sample.created_at for sample in samples]
    index = bisect.bisect_left(times, moment)
    candidates = samples[max(0, index - 1) : index + 1]
    return min(candidates, key=lambda sample: abs(sample.created_at - moment))


def alerts(
    database: Database, check_id: int, start: int | None = None
) -> tuple[int, list[float]]:
    """Decisions over the window and the ticks at which ``attempt_seq`` rose.

    ``start`` is ``attempt_seq`` at T0 (from the baseline B0, the same for
    both databases because the copy was taken at T0); without it the first
    recorded tick is the start.
    """
    history = database.attempts.get(check_id, [])
    if start is not None:
        history = [(since_marker(database), start), *history]
    elif history and history[0][0] != database.baseline_ts:
        # No state row at the first tick: the counter started at 0.
        history = [(database.baseline_ts or 0.0, 0), *history]
    if not history:
        return 0, []
    rises = [ts for (ts, seq), (_, before) in zip(history[1:], history) if seq > before]
    return history[-1][1] - history[0][1], rises


def since_marker(database: Database) -> float:
    return (database.baseline_ts or 0.0) - 1


def load_baseline(path: Path | None) -> dict[int, int] | None:
    """``check_id -> attempt_seq`` from ``shadow_prepare.py``'s B0 file."""
    if path is None:
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(row["check_id"]): int(row["attempt_seq"]) for row in document["baseline"]
    }


def shadow_alerts(log: Path | None) -> Counter[int] | None:
    if log is None:
        return None
    counts: Counter[int] = Counter()
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = SHADOW_ALERT.search(line)
        if match:
            counts[int(match.group(1))] += 1
    return counts


def stuck_claims(database: Database, lease_seconds: float) -> list[dict[str, Any]]:
    stuck: dict[int, dict[str, Any]] = {}
    for ts, rows in database.leases:
        for check_id, status, started, _next, _disabled in rows:
            if status == "processing" and started and ts - started > lease_seconds:
                entry = stuck.setdefault(
                    check_id, {"check_id": check_id, "first_seen": ts, "max_age": 0.0}
                )
                entry["max_age"] = max(entry["max_age"], ts - started)
    return sorted(stuck.values(), key=lambda entry: entry["check_id"])


def retention_findings(
    database: Database, since: float, retention: float, cleanup: float
) -> tuple[list[str], dict[str, Any]]:
    findings: list[str] = []
    summary: dict[str, Any] = {}
    if not database.retention:
        return [f"{database.name}: no retention samples recorded"], summary
    first, last = database.retention[0], database.retention[-1]
    summary = {"first": first[1], "last": last[1]}
    bound = retention + cleanup
    checked = [(ts, sample) for ts, sample in database.retention if ts >= since + bound]
    worst = 0.0
    for ts, sample in checked:
        if sample.get("min_created_at"):
            worst = max(worst, ts - epoch(sample["min_created_at"]))
    summary["checked_ticks"] = len(checked)
    summary["oldest_age_seconds"] = round(worst)
    if checked and worst > bound:
        findings.append(
            f"{database.name}: oldest result was {worst / 3600:.1f} h old, above retention plus one "
            f"cleanup interval ({bound / 3600:.1f} h)"
        )
    if checked and (first[1].get("min_id") or 0) >= (last[1].get("min_id") or 0):
        findings.append(
            f"{database.name}: min(check_result.id) did not advance (cleaner deleted nothing)"
        )
    return findings, summary


def unit_summary(samples: list[tuple[float, dict[str, str]]]) -> dict[str, Any]:
    def number(properties: dict[str, str], key: str) -> int | None:
        value = properties.get(key, "")
        return int(value) if value.isdigit() else None

    memory = [
        value for _, p in samples if (value := number(p, "MemoryCurrent")) is not None
    ]
    peaks = [
        value for _, p in samples if (value := number(p, "MemoryPeak")) is not None
    ]
    cpu = [
        (ts, value)
        for ts, p in samples
        if (value := number(p, "CPUUsageNSec")) is not None
    ]
    restarts = [
        value for _, p in samples if (value := number(p, "NRestarts")) is not None
    ]
    pids = {
        p.get("MainPID") for _, p in samples if p.get("MainPID") not in (None, "", "0")
    }
    summary: dict[str, Any] = {"samples": len(samples), "main_pids": sorted(pids)}
    if memory:
        summary["memory_current_max"] = max(memory)
        summary["memory_current_median"] = sorted(memory)[len(memory) // 2]
    if peaks:
        summary["memory_peak"] = max(peaks)
    if len(cpu) >= 2 and cpu[-1][0] > cpu[0][0]:
        hours = (cpu[-1][0] - cpu[0][0]) / 3600
        summary["cpu_seconds_per_hour"] = round(
            (cpu[-1][1] - cpu[0][1]) / 1e9 / hours, 2
        )
    if restarts:
        summary["restarts"] = max(restarts) - min(restarts)
    return summary


def payload_keys(payload: str | None) -> list[str]:
    try:
        decoded = json.loads(payload or "{}")
    except ValueError:
        return ["<not JSON>"]
    return list(decoded) if isinstance(decoded, dict) else []


def cross_check(path: Path, lease_seconds: float) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        now = int(time.time())
        stuck = connection.execute(
            "SELECT id, ? - processing_started_at FROM health_check"
            " WHERE status = 'processing' AND processing_started_at > 0"
            " AND ? - processing_started_at > ?",
            (now, now, lease_seconds),
        ).fetchall()
        count, oldest, lowest = connection.execute(
            "SELECT COUNT(*), MIN(created_at), MIN(id) FROM check_result"
        ).fetchone()
        stale = connection.execute(
            "SELECT COUNT(*) FROM check_result"
            " WHERE json_extract(data, '$.error_type') = 'stale_processing_lease'"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "stuck_claims": [list(row) for row in stuck],
        "results": count,
        "min_created_at": oldest,
        "min_id": lowest,
        "stale_processing_lease_results": stale,
    }


def compare(args: argparse.Namespace) -> dict[str, Any]:
    since = epoch(args.since)
    databases, units = load(args.record, since)
    prod, shadow = databases["prod"], databases["shadow"]
    checks = compared_checks(prod, shadow)
    log_alerts = shadow_alerts(args.shadow_log)
    baseline = load_baseline(args.baseline)
    report: dict[str, Any] = {"since": utc(since), "checks": {}, "findings": []}
    findings: list[str] = report["findings"]
    for name, database in databases.items():
        if database.errors:
            findings.append(
                f"{name}: {len(database.errors)} unreadable tick(s), first: {database.errors[0][1]}"
            )
    if not checks:
        findings.append(
            "no check is enabled in both databases (empty or incomplete record)"
        )

    for check_id in checks:
        check_type, check_name = prod.checks.get(check_id, ["?", "?"])[:2]
        p_samples, s_samples = (
            prod.samples.get(check_id, []),
            shadow.samples.get(check_id, []),
        )
        entry: dict[str, Any] = {
            "type": check_type,
            "name": check_name,
            "samples": {"prod": len(p_samples), "shadow": len(s_samples)},
            "outcomes": {
                name: dict(Counter(f"{s.status}/{s.error_type}" for s in samples))
                for name, samples in (("prod", p_samples), ("shadow", s_samples))
            },
        }
        disagreements = []
        for sample in s_samples:
            other = nearest(p_samples, sample.created_at)
            if other is None:
                continue
            if (sample.status, sample.error_type) != (other.status, other.error_type):
                disagreements.append(
                    {
                        "shadow": [
                            utc(sample.created_at),
                            sample.status,
                            sample.error_type,
                        ],
                        "prod": [utc(other.created_at), other.status, other.error_type],
                        "apart_seconds": round(
                            abs(sample.created_at - other.created_at)
                        ),
                    }
                )
        entry["disagreements"] = disagreements
        if disagreements:
            findings.append(
                f"check {check_id} ({check_type}): {len(disagreements)} status/error-type disagreement(s)"
            )
        if not s_samples:
            findings.append(
                f"check {check_id} ({check_type}): no shadow result in the window"
            )

        key_sets: dict[str, dict[str, set[tuple[str, ...]]]] = {}
        for name, samples in (("prod", p_samples), ("shadow", s_samples)):
            grouped: dict[str, set[tuple[str, ...]]] = defaultdict(set)
            for sample in samples:
                grouped[f"{sample.status}/{sample.error_type}"].add(sample.keys)
            key_sets[name] = grouped
        key_differences = []
        for outcome in sorted(set(key_sets["prod"]) & set(key_sets["shadow"])):
            only_prod = key_sets["prod"][outcome] - key_sets["shadow"][outcome]
            only_shadow = key_sets["shadow"][outcome] - key_sets["prod"][outcome]
            if only_prod or only_shadow:
                key_differences.append(
                    {
                        "outcome": outcome,
                        "only_prod": sorted(list(keys) for keys in only_prod),
                        "only_shadow": sorted(list(keys) for keys in only_shadow),
                    }
                )
        entry["key_differences"] = key_differences
        if key_differences:
            findings.append(
                f"check {check_id} ({check_type}): data keys differ for {len(key_differences)} outcome(s)"
            )

        start = None if baseline is None else baseline.get(check_id, 0)
        p_alerts, p_times = alerts(prod, check_id, start)
        s_alerts, s_times = alerts(shadow, check_id, start)
        entry["alerts"] = {
            "prod": p_alerts,
            "shadow": s_alerts,
            "prod_times": [utc(ts) for ts in p_times],
            "shadow_times": [utc(ts) for ts in s_times],
        }
        if log_alerts is not None:
            entry["alerts"]["shadow_log"] = log_alerts.get(check_id, 0)
            if log_alerts.get(check_id, 0) != s_alerts:
                findings.append(
                    f"check {check_id}: {log_alerts.get(check_id, 0)} shadow notification line(s) "
                    f"but attempt_seq rose by {s_alerts}"
                )
        if p_alerts != s_alerts:
            findings.append(
                f"check {check_id} ({check_type}): {p_alerts} production vs {s_alerts} shadow alert(s)"
            )
        report["checks"][check_id] = entry

    for name, database in databases.items():
        stale = [
            [check_id, utc(sample.created_at)]
            for check_id, samples in sorted(database.samples.items())
            for sample in samples
            if sample.error_type == "stale_processing_lease"
        ]
        report.setdefault("stale_processing_lease", {})[name] = stale
        stuck = stuck_claims(database, args.lease_seconds)
        report.setdefault("stuck_claims", {})[name] = stuck
        if stuck:
            findings.append(
                f"{name}: {len(stuck)} claim(s) stayed in processing beyond {args.lease_seconds:g} s"
            )
        report.setdefault("incidents", {})[name] = {
            key: {"alert_count": row[3], "payload_keys": payload_keys(row[4])}
            for key, row in sorted(database.incidents.items())
        }
    if report["stale_processing_lease"]["shadow"]:
        findings.append(
            f"shadow: {len(report['stale_processing_lease']['shadow'])} stale_processing_lease result(s)"
        )

    retention, summary = retention_findings(
        shadow, since, args.retention_seconds, args.cleanup_interval
    )
    findings.extend(retention)
    report["retention"] = {"shadow": summary}

    report["units"] = {
        unit: unit_summary(samples) for unit, samples in sorted(units.items())
    }
    for unit, summary in report["units"].items():
        if summary.get("restarts"):
            findings.append(f"unit {unit}: restarted {summary['restarts']} time(s)")
    if args.shadow_unit in report["units"]:
        summary = report["units"][args.shadow_unit]
        if summary.get("memory_current_median", 0) > RSS_BUDGET:
            findings.append(
                f"unit {args.shadow_unit}: median memory above the 20 MiB budget (PLAN.md 4.4)"
            )
        if summary.get("memory_peak", 0) > PEAK_BUDGET:
            findings.append(
                f"unit {args.shadow_unit}: memory peak above the 40 MiB budget (PLAN.md 4.4)"
            )

    for name, path in (("prod", args.prod_db), ("shadow", args.shadow_db)):
        if path is not None:
            direct = cross_check(path, args.lease_seconds)
            report.setdefault("cross_check", {})[name] = direct
            if direct["stuck_claims"]:
                findings.append(
                    f"{name} (direct query): {len(direct['stuck_claims'])} stuck claim(s) now"
                )
    return report


def render(report: dict[str, Any]) -> Iterable[str]:
    yield f"# Shadow comparison since {report['since']} UTC"
    yield ""
    yield "| check | type | samples prod/shadow | alerts prod/shadow | disagreements | key diffs |"
    yield "| --- | --- | --- | --- | --- | --- |"
    for check_id, entry in report["checks"].items():
        yield (
            f"| {check_id} {entry['name']} | {entry['type']} | {entry['samples']['prod']}/{entry['samples']['shadow']}"
            f" | {entry['alerts']['prod']}/{entry['alerts']['shadow']} | {len(entry['disagreements'])}"
            f" | {len(entry['key_differences'])} |"
        )
    for check_id, entry in report["checks"].items():
        if not (entry["disagreements"] or entry["key_differences"]):
            continue
        yield ""
        yield f"## check {check_id} ({entry['type']}) {entry['name']}"
        yield f"- outcomes prod: {entry['outcomes']['prod']}"
        yield f"- outcomes shadow: {entry['outcomes']['shadow']}"
        for item in entry["disagreements"]:
            yield (
                f"- shadow {item['shadow'][0]} {item['shadow'][1]}/{item['shadow'][2]} vs prod "
                f"{item['prod'][0]} {item['prod'][1]}/{item['prod'][2]} ({item['apart_seconds']} s apart)"
            )
        for item in entry["key_differences"]:
            yield f"- keys {item['outcome']}: only prod {item['only_prod']}, only shadow {item['only_shadow']}"
    yield ""
    yield "## Leases, cleaner, incidents, units"
    yield f"- stale_processing_lease results: {report['stale_processing_lease']}"
    yield f"- claims stuck beyond the lease: {report['stuck_claims']}"
    yield f"- shadow retention: {report['retention']['shadow']}"
    yield f"- collector incidents: {report['incidents']}"
    for unit, summary in report["units"].items():
        yield f"- unit {unit}: {summary}"
    for name, direct in report.get("cross_check", {}).items():
        yield f"- direct query {name}: {direct}"
    yield ""
    yield f"## Findings to explain ({len(report['findings'])})"
    for finding in report["findings"]:
        yield f"- {finding}"
    if not report["findings"]:
        yield "- none"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--record", required=True, type=Path, help="JSONL from shadow_record.py"
    )
    parser.add_argument(
        "--since", required=True, help="T0: UTC 'YYYY-MM-DD HH:MM:SS' or Unix epoch"
    )
    parser.add_argument(
        "--baseline", type=Path, help="B0 from shadow_prepare.py (attempt_seq at T0)"
    )
    parser.add_argument(
        "--shadow-log", type=Path, help="shadow agent journal (notification lines)"
    )
    parser.add_argument(
        "--prod-db", type=Path, help="cross-check the production database directly"
    )
    parser.add_argument(
        "--shadow-db", type=Path, help="cross-check the shadow database directly"
    )
    parser.add_argument(
        "--lease-seconds", type=float, default=900, help="effective processing lease"
    )
    parser.add_argument(
        "--retention-seconds", type=float, default=86400, help="--retention-period"
    )
    parser.add_argument(
        "--cleanup-interval", type=float, default=3600, help="--cleanup-interval"
    )
    parser.add_argument(
        "--shadow-unit",
        default="nyxmon-agent-shadow",
        help="unit checked against PLAN.md 4.4",
    )
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    parser.add_argument(
        "--fail-on-findings", action="store_true", help="exit 1 when findings remain"
    )
    args = parser.parse_args(argv)
    try:
        epoch(args.since)
    except ValueError:
        parser.error("--since must be 'YYYY-MM-DD HH:MM:SS' (UTC) or a Unix epoch")
    if not args.record.is_file():
        print(f"record not found: {args.record}", file=sys.stderr)
        return 1
    report = compare(args)
    for line in render(report):
        print(line)
    if args.json is not None:
        args.json.write_text(
            json.dumps(report, indent=1, sort_keys=True), encoding="utf-8"
        )
    return 1 if args.fail_on_findings and report["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
