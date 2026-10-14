"""Shadow-run tooling of the Rust agent (agent-rs/parity, PLAN.md 9.1).

``shadow_prepare.py`` makes the database copy, ``shadow_record.py`` records
both databases over time, ``shadow_compare.py`` reports the differences.
These tests run the three on databases built from the schema fixture.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

PARITY = Path(__file__).resolve().parents[2] / "agent-rs" / "parity"
SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "agent-rs"
    / "fixtures"
    / "schema"
    / "schema.sql"
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, PARITY / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load("shadow_prepare")
recorder = _load("shadow_record")
compare = _load("shadow_compare")


def _created(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


def _production(path: Path, now: int) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA.read_text())
    connection.execute("INSERT INTO service (id, name) VALUES (1, 'svc')")
    for check_id, check_type, status, started in (
        (1, "http", "processing", now - 30),
        (2, "smtp", "idle", 0),
        (3, "imap", "idle", 0),
        (4, "tcp", "idle", 0),
        (5, "http", "idle", 0),
    ):
        connection.execute(
            "INSERT INTO health_check (id, service_id, name, check_type, url, check_interval,"
            " next_check_time, processing_started_at, status, disabled, data)"
            " VALUES (?, 1, ?, ?, '', 60, ?, ?, ?, 0, '{}')",
            (check_id, f"check {check_id}", check_type, now + 60, started, status),
        )
    connection.execute(
        "INSERT INTO check_notification_state (check_id, failure_count, attempt_seq) VALUES (1, 2, 3)"
    )
    connection.execute(
        "INSERT INTO collector_incident (incident_key, alert_count, payload)"
        " VALUES ('collector:stale_processing_lease', 1, '{}')"
    )
    connection.execute(
        "INSERT INTO check_result (health_check_id, status, data, created_at) VALUES (1, 'ok', '{}', ?)",
        (_created(now - 7200),),
    )
    connection.commit()
    connection.close()


def test_prepare_copies_and_neutralises_the_copy(tmp_path, capsys):
    now = int(time.time())
    source, dest = tmp_path / "prod.sqlite3", tmp_path / "shadow" / "db.sqlite3"
    dest.parent.mkdir()
    _production(source, now)
    before = source.read_bytes()

    assert prepare.main(["--source", str(source), "--dest", str(dest)]) == 0
    assert source.read_bytes() == before  # the source is only read
    copy = sqlite3.connect(dest)
    assert copy.execute(
        "SELECT id FROM health_check WHERE disabled = 1 ORDER BY id"
    ).fetchall() == [(2,), (3,)]
    assert copy.execute(
        "SELECT status, processing_started_at, next_check_time FROM health_check WHERE id = 1"
    ).fetchone() == (
        "idle",
        0,
        now + 60,
    )
    assert copy.execute("SELECT COUNT(*) FROM collector_incident").fetchone() == (0,)
    assert copy.execute("SELECT COUNT(*) FROM check_result").fetchone() == (1,)
    copy.close()
    baseline = json.loads(Path(f"{dest}.baseline.json").read_text())
    assert baseline["mail_checks_disabled"] == 2
    assert baseline["claims_released"] == 1
    assert baseline["incidents_deleted"] == 1
    assert baseline["enabled_checks"] == {"http": 2, "tcp": 1}
    assert baseline["baseline"][0]["attempt_seq"] == 3
    assert (Path(f"{dest}.baseline.json").stat().st_mode & 0o777) == 0o600
    assert f"T0={baseline['t0']} " in capsys.readouterr().out

    # Never overwrites an existing copy.
    assert prepare.main(["--source", str(source), "--dest", str(dest)]) == 1


def _insert(path: Path, check_id: int, status: str, data: dict, created: float) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO check_result (health_check_id, status, data, created_at) VALUES (?, ?, ?, ?)",
        (check_id, status, json.dumps(data), _created(created)),
    )
    connection.commit()
    connection.close()


def _execute(path: Path, sql: str, *params: object) -> None:
    connection = sqlite3.connect(path)
    connection.execute(sql, params)
    connection.commit()
    connection.close()


@pytest.fixture
def recorded(tmp_path):
    """Two ticks of a shadow run in which check 4 disagrees once."""
    now = int(time.time())
    prod, shadow = tmp_path / "prod.sqlite3", tmp_path / "shadow.sqlite3"
    _production(prod, now)
    assert prepare.main(["--source", str(prod), "--dest", str(shadow)]) == 0
    # Production's claim is still running; the copy's was released. The
    # shadow agent decides one alert for check 5 before the first tick.
    _execute(
        shadow,
        "INSERT INTO check_notification_state (check_id, attempt_seq) VALUES (5, 1)",
    )
    t0 = now - 600
    recorders = [
        recorder.DatabaseRecorder("prod", prod, recorder.since_text(str(t0))),
        recorder.DatabaseRecorder("shadow", shadow, recorder.since_text(str(t0))),
    ]
    out = tmp_path / "record.jsonl"
    lines = [r.tick(now - 120) for r in recorders]

    ok = {"duration_ms": 3}
    refused = {"error_type": "connection_error", "error_msg": "refused", "host": "h"}
    for path in (prod, shadow):
        _insert(path, 4, "ok", ok, now - 100)
        _insert(path, 5, "ok", ok, now - 100)
    _insert(prod, 4, "ok", ok, now - 50)
    _insert(shadow, 4, "error", refused, now - 48)
    _insert(shadow, 5, "ok", {"duration_ms": 1, "extra": 1}, now - 40)
    _insert(prod, 5, "ok", ok, now - 41)
    _execute(
        shadow,
        "INSERT INTO check_notification_state (check_id, failure_count, attempt_seq) VALUES (4, 1, 1)",
    )
    lines += [r.tick(now) for r in recorders]
    out.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return out, t0, prod, shadow


def test_recorder_writes_incremental_snapshots(recorded):
    out, t0, _prod, _shadow = recorded
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    first_prod, first_shadow, second_prod, second_shadow = lines
    assert first_prod["checks"]["2"][0] == "smtp"
    assert first_prod["results"] == []  # the 2-hour-old row is before T0
    assert first_prod["states"][0][:2] == [1, 2]
    assert [row[1] for row in first_prod["leases"]][:1] == ["processing"]
    assert first_shadow["leases"][0][1] == "idle"
    assert "checks" not in second_prod
    assert [row[1:3] for row in second_shadow["results"]] == [
        [4, "ok"],
        [5, "ok"],
        [4, "error"],
        [5, "ok"],
    ]
    assert second_shadow["results"][2][4]["error_type"] == "connection_error"
    assert second_prod["states"] == []  # unchanged rows are not repeated
    assert [row[0] for row in second_shadow["states"]] == [4]
    assert second_prod["retention"]["count"] == 5  # one row from before T0


def test_compare_reports_disagreements_keys_alerts_and_claims(
    recorded, tmp_path, capsys
):
    out, t0, prod, shadow = recorded
    report_path = tmp_path / "report.json"
    log = tmp_path / "shadow.log"
    alert = (
        " macmini nyxmon-agent[1]: 2026-10-15 10:00:00,000 - nyxmon.adapters.notification"
        " - ERROR - notification check_id=4 status=error error_type=connection_error\n"
    )
    # `journalctl -o short-unix`: only lines inside the recorded window count.
    log.write_text(f"{t0 - 3600}.5{alert}{time.time() - 30:.6f}{alert}")
    code = compare.main(
        [
            "--record",
            str(out),
            "--since",
            str(t0),
            "--json",
            str(report_path),
            "--shadow-log",
            str(log),
            "--lease-seconds",
            "10",
            "--prod-db",
            str(prod),
            "--shadow-db",
            str(shadow),
            "--fail-on-findings",
        ]
    )
    assert code == 1
    report = json.loads(report_path.read_text())
    # Mail checks are disabled in the copy and never compared.
    assert sorted(report["checks"]) == ["1", "4", "5"]
    check4 = report["checks"]["4"]
    assert check4["outcomes"] == {
        "prod": {"ok/": 2},
        "shadow": {"ok/": 1, "error/connection_error": 1},
    }
    assert len(check4["disagreements"]) == 1
    assert check4["disagreements"][0]["shadow"][1:] == ["error", "connection_error"]
    assert check4["disagreements"][0]["prod"][1:] == ["ok", ""]
    assert check4["disagreements"][0]["apart_seconds"] == 2
    assert check4["alerts"] == {
        "prod": 0,
        "shadow": 1,
        "prod_times": [],
        "shadow_times": [check4["alerts"]["shadow_times"][0]],
        "shadow_log": 1,
        "shadow_log_after_window": 0,
    }
    assert report["checks"]["5"]["key_differences"] == [
        {"outcome": "ok/", "only_prod": [], "only_shadow": [["duration_ms", "extra"]]}
    ]
    # Production's claim (30 s old, lease 10 s) is reported as stuck.
    assert [entry["check_id"] for entry in report["stuck_claims"]["prod"]] == [1]
    assert report["stuck_claims"]["shadow"] == []
    assert report["cross_check"]["prod"]["stuck_claims"][0][0] == 1
    findings = "\n".join(report["findings"])
    assert "check 4 (tcp): 1 status/error-type disagreement(s)" in findings
    assert "check 4 (tcp): 0 production vs 1 shadow alert(s)" in findings
    assert "check 5 (http): data keys differ for 1 outcome(s)" in findings
    assert "prod: 1 claim(s) stayed in processing beyond 10 s" in findings
    assert "## Findings to explain" in capsys.readouterr().out


def _synthetic(tmp_path, ticks, retention, units=None, results=None):
    """A record with one http check and the given retention samples."""
    lines = []
    for index, (ts, sample) in enumerate(zip(ticks, retention)):
        for name in ("prod", "shadow"):
            line = {
                "ts": ts,
                "db": name,
                "results": (results or {}).get((index, name), []),
                "states": [],
                "incidents": [],
                "leases": [[1, "idle", 0, 0, 0]],
                "retention": sample,
            }
            if index == 0:
                line["checks"] = {"1": ["http", "c", 60]}
            lines.append(line)
        if units is not None:
            lines.append({"ts": ts, "units": units[index]})
    record = tmp_path / "record.jsonl"
    record.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return record


def _findings(tmp_path, record, since, *extra):
    report = tmp_path / "report.json"
    args = ["--record", str(record), "--since", str(since), "--json", str(report)]
    assert compare.main([*args, *extra]) == 0
    return json.loads(report.read_text())["findings"]


def test_a_24_hour_window_still_checks_the_cleaner(tmp_path):
    # PLAN.md 9.1 phase A lasts 24 h: the cleaner must be verified within it.
    since, day = 1_000_000, 86400
    stale = {"count": 5, "min_created_at": _created(since - 2 * day), "min_id": 1}
    record = _synthetic(tmp_path, [since, since + 7200, since + day], [stale] * 3)
    findings = _findings(tmp_path, record, since)
    assert any("shadow: oldest result was 72.0 h old" in f for f in findings)
    assert any("shadow: min(check_result.id) did not advance" in f for f in findings)

    healthy = [
        {"count": 5, "min_created_at": _created(ts - day), "min_id": 1 + index}
        for index, ts in enumerate([since, since + 7200, since + day])
    ]
    record = _synthetic(tmp_path, [since, since + 7200, since + day], healthy)
    findings = _findings(tmp_path, record, since)
    assert not [f for f in findings if "oldest result" in f or "advance" in f]


def test_a_short_recording_reports_the_cleaner_unverified(tmp_path):
    since = 1_000_000
    sample = {"count": 1, "min_created_at": _created(since), "min_id": 1}
    record = _synthetic(tmp_path, [since, since + 60], [sample] * 2)
    findings = _findings(tmp_path, record, since)
    assert any("cleaner not verified" in f for f in findings)


def test_missing_production_samples_and_stopped_units_are_findings(tmp_path):
    since = 1_000_000
    sample = {"count": 1, "min_created_at": _created(since), "min_id": 1}
    result = [[10, 1, "ok", _created(since + 30), {"duration_ms": 1}]]
    running = {
        "ActiveState": "active",
        "MainPID": "42",
        "MemoryCurrent": "1000",
        "MemoryPeak": "2000",
        "NRestarts": "0",
    }
    failed = {"ActiveState": "failed", "MainPID": "0", "NRestarts": "0"}
    record = _synthetic(
        tmp_path,
        [since, since + 60],
        [sample] * 2,
        units=[{"nyxmon-agent-shadow": running}, {"nyxmon-agent-shadow": failed}],
        results={(1, "shadow"): result},
    )
    findings = _findings(tmp_path, record, since)
    assert "check 1 (http): no production result in the window" in findings
    assert any(
        f.startswith("unit nyxmon-agent-shadow: not running in 1 of 2 sample(s)")
        for f in findings
    )

    # A unit without memory samples cannot pass the PLAN.md 4.4 budgets.
    record = _synthetic(
        tmp_path,
        [since],
        [sample],
        units=[{"nyxmon-agent-shadow": {"ActiveState": "active", "MainPID": "42"}}],
    )
    findings = _findings(tmp_path, record, since)
    assert any("memory not measured" in f for f in findings)
    record = _synthetic(tmp_path, [since], [sample])
    assert any("not sampled" in f for f in _findings(tmp_path, record, since))


def test_untimed_journal_lines_are_flagged(tmp_path):
    since = 1_000_000
    sample = {"count": 1, "min_created_at": _created(since), "min_id": 1}
    record = _synthetic(tmp_path, [since, since + 60], [sample] * 2)
    log = tmp_path / "shadow.log"
    log.write_text("notification check_id=1 status=error error_type=x\n")
    findings = _findings(tmp_path, record, since, "--shadow-log", str(log))
    assert any("without a Unix timestamp" in f for f in findings)


def test_compare_flags_a_cleaner_that_deleted_nothing(tmp_path):
    since = 1_000_000
    day = 86400
    lines = []
    for index, ts in enumerate((since, since + day + 7200)):
        for name in ("prod", "shadow"):
            line = {
                "ts": ts,
                "db": name,
                "results": [],
                "states": [],
                "incidents": [],
                "leases": [[1, "idle", 0, 0, 0]],
                "retention": {
                    "count": 5,
                    "min_created_at": _created(since - day),
                    "min_id": 1,
                },
            }
            if index == 0:
                line["checks"] = {"1": ["http", "c", 60]}
            lines.append(line)
    record = tmp_path / "record.jsonl"
    record.write_text("".join(json.dumps(line) + "\n" for line in lines))
    report = tmp_path / "report.json"
    assert (
        compare.main(
            ["--record", str(record), "--since", str(since), "--json", str(report)]
        )
        == 0
    )
    findings = json.loads(report.read_text())["findings"]
    assert any("oldest result was" in finding for finding in findings)
    assert any("did not advance" in finding for finding in findings)


def test_alert_counts_start_from_the_baseline(recorded, tmp_path):
    out, t0, _prod, shadow = recorded
    without, with_b0 = tmp_path / "without.json", tmp_path / "with.json"
    assert (
        compare.main(["--record", str(out), "--since", str(t0), "--json", str(without)])
        == 0
    )
    baseline = f"{shadow}.baseline.json"
    assert (
        compare.main(
            [
                "--record",
                str(out),
                "--since",
                str(t0),
                "--baseline",
                baseline,
                "--json",
                str(with_b0),
            ]
        )
        == 0
    )
    # The first tick already saw attempt_seq 1; only B0 (0 for check 5) counts it.
    assert json.loads(without.read_text())["checks"]["5"]["alerts"]["shadow"] == 0
    assert json.loads(with_b0.read_text())["checks"]["5"]["alerts"]["shadow"] == 1
    assert json.loads(with_b0.read_text())["checks"]["5"]["alerts"]["prod"] == 0


def test_a_failed_tick_loses_nothing(tmp_path):
    now = int(time.time())
    prod = tmp_path / "prod.sqlite3"
    _production(prod, now)
    db = recorder.DatabaseRecorder("prod", prod, recorder.since_text(str(now - 600)))
    assert "error" not in db.tick(now - 60)
    _insert(prod, 4, "ok", {"duration_ms": 1}, now - 30)
    _execute(
        prod, "UPDATE check_notification_state SET attempt_seq = 4 WHERE check_id = 1"
    )
    # The incident table cannot be read in this tick (after results and
    # states were read); the tick fails as a whole.
    _execute(prod, "ALTER TABLE collector_incident RENAME TO collector_incident_moved")
    failed = db.tick(now - 20)
    assert "error" in failed and "results" not in failed
    _execute(prod, "ALTER TABLE collector_incident_moved RENAME TO collector_incident")
    retried = db.tick(now)
    assert [row[1] for row in retried["results"]] == [4]
    assert [row[0] for row in retried["states"]] == [1]


def test_an_alert_logged_while_the_last_snapshot_is_read_still_matches(tmp_path):
    # The recorder reads production, then shadow; a decision the shadow
    # snapshot sees can be logged just after that snapshot began.
    since, tick = 1_000_000, 1_000_100
    sample = {"count": 1, "min_created_at": _created(since), "min_id": 1}
    result = [[10, 1, "error", _created(since + 30), {"error_type": "x"}]]
    lines = []
    for ts, seq in ((since, 0), (tick, 1)):
        for name in ("prod", "shadow"):
            line = {
                "ts": ts,
                "db": name,
                "read_at": ts + (0.02 if name == "shadow" else 0.0),
                "results": result if (ts == tick) else [],
                "states": [[1, 1, 1, 0, 1, 1, 0, seq, 0]],
                "incidents": [],
                "leases": [[1, "idle", 0, 0, 0]],
                "retention": sample,
            }
            if ts == since:
                line["checks"] = {"1": ["http", "c", 60]}
            lines.append(line)
    record = tmp_path / "record.jsonl"
    record.write_text("".join(json.dumps(line) + "\n" for line in lines))
    log = tmp_path / "shadow.log"
    alert = " host agent[1]: notification check_id=1 status=error error_type=x\n"

    def mismatch(text):
        log.write_text(text)
        findings = _findings(tmp_path, record, since, "--shadow-log", str(log))
        return [f for f in findings if "shadow notification line(s)" in f]

    # Logged 31 ms after the shadow snapshot began: still that decision.
    assert mismatch(f"{tick + 0.051}{alert}") == []
    # Logged inside the window: matches too.
    assert mismatch(f"{since + 50}{alert}") == []
    # No line for a decision the snapshot saw: a finding.
    assert mismatch("") != []
    # Two lines for one decision inside the window: a finding.
    assert mismatch(f"{since + 50}{alert}{since + 60}{alert}") != []
    # A line far after the window belongs to a later decision: a finding.
    assert mismatch(f"{tick + 3600}{alert}") != []
