"""Offline parity harness: Python and Rust agents on identical inputs.

PLAN.md 7.4. Two copies of a database seeded with the same check corpus;
``start-agent`` runs on copy A and ``nyxmon-agent --shadow`` on copy B at the
same time, each against its own scripted fake servers (``fakes.py``). The
fake servers answer by request index per endpoint, so the n-th sample of a
check sees the same response in both runs. Once every enabled check has at
least ``SAMPLES`` results in both databases, both agents get SIGTERM and the
harness compares, per check:

- the first ``SAMPLES`` results: status, data key order and values, with
  only the documented masks applied (``MASKED_KEYS``, ``P2_ERROR_MSG``,
  PLAN.md 1.3 P2 and 1.5 D-12);
- the ``check_notification_state`` row observed while the check had exactly
  k results, k = 1..SAMPLES (every column except ``attempt_at``; timestamps
  as zero/non-zero), and ``attempt_at`` = 0 at the end for checks whose
  script ends OK;
- the notifications as the ordered list of ``(status, error_type)`` from the
  Python ``LoggingNotifier`` lines and the Rust ``--shadow`` lines;
- disabled checks (no result, still idle) and ``collector_incident`` rows.

Every unmasked difference fails the run (exit 1). Run with
``just rs-parity`` (builds the Rust binary first) or::

    uv run python agent-rs/parity/run_parity.py --rust-binary PATH

The DNS checks need a nameserver on port 53 (neither agent can query another
port): the harness binds ``--dns-address`` (default 127.0.0.1) port 53, which
needs ``net.ipv4.ip_unprivileged_port_start <= 53`` on Linux (CI sets it).
When the port cannot be bound the DNS checks are left out with a notice, or
the run fails with ``--require-dns``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import (
    FAKE_PING,
    DnsFake,
    HttpFake,
    ImapFake,
    SmtpFake,
    TcpFake,
)

REPO = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO / "agent-rs" / "fixtures" / "schema" / "schema.sql"

#: Results compared per check (every script ends OK within this many).
SAMPLES = 6
#: Check interval of every check, in seconds.
INTERVAL = 2

#: Result data keys whose values are masked (presence and position are
#: still compared): timestamps and durations (P2), the SMTP subject and
#: correlation token (random, D-12).
MASKED_KEYS = {"created_at", "latest_internaldate", "subject", "token"}
MASKED_SUFFIXES = ("_ms", "_at")

#: ``(check_type, error_type)`` whose ``error_msg`` is ``str(exc)`` of a
#: library exception (PLAN.md 1.3 P2); the error type is still compared.
P2_ERROR_MSG = {
    ("http", "timeout"),
    ("http", "connection_error"),
    ("http", "request_error"),
    ("json-http", "timeout"),
    ("json-http", "connection_error"),
    ("json-metrics", "timeout"),
    ("json-metrics", "connection_error"),
    ("tcp", "tls_error"),
    ("tcp", "connection_error"),
}

STATE_COLUMNS = (
    "failure_count",
    "last_attempt_count",
    "attempt_seq",
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
    "held_since",
)
#: Timestamps compared as zero/non-zero.
STATE_TIMESTAMPS = {
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
    "held_since",
}


# ------------------------------------------------------------------ corpus


def _openssl_cert(directory: Path, name: str, days: int) -> ssl.SSLContext:
    key, cert = directory / f"{name}.key", directory / f"{name}.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            str(days),
            "-subj",
            "/CN=parity.test",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


@dataclass
class Check:
    check_id: int
    name: str
    check_type: str
    url: str
    data: dict[str, Any]
    disabled: bool = False
    #: The script ends with an OK result (``attempt_at`` must be 0 at the end).
    ends_ok: bool = True


def ok(body: Any = "ok", status: int = 200, **extra: Any) -> dict[str, Any]:
    return {"status": status, "body": body, **extra}


THRESHOLDS = [
    {"path": "$.cpu", "op": "<", "value": 80, "severity": "warning"},
    {"path": "$.disk", "op": "<", "value": 90, "severity": "critical"},
]
HEALTHY = {"cpu": 10, "disk": 10}
WARN = {"cpu": 85, "disk": 10}
CRIT = {"cpu": 85, "disk": 95}


@dataclass
class Agent:
    """The fake servers of one agent and the corpus pointing at them."""

    label: str
    workdir: Path
    contexts: dict[str, ssl.SSLContext]
    dns: DnsFake | None
    http: HttpFake = field(init=False)
    tcp: dict[int, TcpFake] = field(init=False, default_factory=dict)
    smtp: SmtpFake = field(init=False)
    imap: ImapFake = field(init=False)
    ping_state: Path = field(init=False)
    checks: list[Check] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.http = HttpFake()
        self.smtp = SmtpFake(
            ["ok", "rcpt_refused", "mail_temporary", "data_rejected", "ok"]
        )
        self.imap = ImapFake(["found", "empty", "empty", "select_no", "found"])
        self.ping_state = self.workdir / f"ping-{self.label}"
        self.ping_state.mkdir()
        self.checks = self._corpus()

    def _http(self, check_id: int, script: list[dict[str, Any]], path: str = "") -> str:
        key = f"/c/{check_id}{path}"
        self.http.script.set(key, script)
        return f"http://127.0.0.1:{self.http.port}{key}"

    def _tcp(self, check_id: int, actions: list[str]) -> int:
        fake = TcpFake(self.contexts, actions)
        self.tcp[check_id] = fake
        return fake.port

    def _corpus(self) -> list[Check]:
        checks = [
            Check(
                1,
                "http flapping",
                "http",
                self._http(
                    1, [ok(), ok(status=500), ok(status=500), ok(status=500), ok()]
                ),
                {},
            ),
            Check(
                2,
                "http expected status, threshold 1",
                "http",
                self._http(
                    2,
                    [
                        ok(status=204),
                        ok(),
                        ok(status=204),
                        ok(status=503),
                        ok(status=204),
                    ],
                ),
                {
                    "expected_status": 204,
                    "notification_policy": {"consecutive_failures": 1},
                },
            ),
            Check(
                3,
                "http redirect location",
                "http",
                self._http(
                    3,
                    [
                        ok(
                            status=301, headers={"Location": "https://example.test/new"}
                        ),
                        ok(
                            status=301, headers={"Location": "https://example.test/old"}
                        ),
                        ok(
                            status=301, headers={"Location": "https://example.test/old"}
                        ),
                        ok(
                            status=301, headers={"Location": "https://example.test/new"}
                        ),
                    ],
                ),
                {
                    "follow_redirects": False,
                    "expected_status": 301,
                    "expected_location": "https://example.test/new",
                },
            ),
            Check(
                4,
                "http timeout",
                "http",
                self._http(4, [ok(), ok(delay=2.5), ok(delay=2.5), ok()]),
                {"timeout": 1, "retries": 0},
            ),
            Check(
                5,
                "json-http",
                "json-http",
                self._http(5, [ok({"a": 1}), ok(status=502), ok({"a": 1})]),
                {},
            ),
            Check(
                6,
                "json-metrics thresholds",
                "json-metrics",
                "",
                {
                    "url": self._http(
                        6,
                        [
                            ok(HEALTHY),
                            ok(WARN),
                            ok(WARN),
                            ok(CRIT),
                            ok("not json"),
                            ok(HEALTHY),
                        ],
                    ),
                    "checks": THRESHOLDS,
                    "retries": 0,
                },
            ),
            Check(
                7,
                "json-metrics suppressed",
                "json-metrics",
                "",
                {
                    "url": self._http(
                        7, [ok(HEALTHY), ok(CRIT), ok(CRIT), ok(CRIT), ok(HEALTHY)]
                    ),
                    "checks": THRESHOLDS,
                    "retries": 0,
                    "notification_policy": {"consecutive_failures": 1},
                    "notification_suppression": {
                        "url": self._http(
                            7,
                            [
                                ok({"state": "running"}),
                                ok({"state": "idle"}),
                                ok({"state": "running"}),
                            ],
                            path="/maintenance",
                        ),
                        "status_path": "$.state",
                        "active_statuses": ["running"],
                    },
                },
            ),
            Check(
                8,
                "json-metrics warning policy",
                "json-metrics",
                "",
                {
                    "url": self._http(
                        8,
                        [
                            ok(HEALTHY),
                            ok(WARN),
                            ok(HEALTHY),
                            ok(WARN),
                            ok(WARN),
                            ok(HEALTHY),
                        ],
                    ),
                    "checks": THRESHOLDS,
                    "retries": 0,
                    "notification_policy": {"warning_consecutive_failures": 1},
                },
            ),
            Check(
                9,
                "tcp plain",
                "tcp",
                "",
                {"host": "127.0.0.1", "port": self._tcp(9, ["plain"])},
            ),
            Check(
                10,
                "tcp certificate expiry",
                "tcp",
                "",
                {
                    "host": "127.0.0.1",
                    "port": self._tcp(
                        10,
                        ["tls:long", "tls:short", "tls:short", "tls:short", "tls:long"],
                    ),
                    "tls_mode": "implicit",
                    "verify": False,
                    "check_cert_expiry": True,
                    "min_cert_days": 30,
                    "retries": 0,
                },
            ),
            Check(
                11,
                "tcp TLS handshake failure",
                "tcp",
                "",
                {
                    "host": "127.0.0.1",
                    "port": self._tcp(
                        11, ["tls:long", "garbage", "garbage", "tls:long"]
                    ),
                    "tls_mode": "implicit",
                    "verify": False,
                    "retries": 0,
                },
            ),
            Check(
                19,
                "tcp TLS server closing without close_notify",
                "tcp",
                "",
                {
                    "host": "127.0.0.1",
                    "port": self._tcp(19, ["tls-abrupt:long"]),
                    "tls_mode": "implicit",
                    "verify": False,
                    "retries": 0,
                },
                ends_ok=False,
            ),
            Check(
                13,
                "smtp",
                "smtp",
                "",
                {
                    "host": "127.0.0.1",
                    "port": self.smtp.port,
                    "tls": "none",
                    "from_addr": "monitor@parity.test",
                    "to_addr": "probe@parity.test",
                    "retries": 0,
                    "timeout": 5,
                },
            ),
            Check(
                14,
                "imap",
                "imap",
                "",
                {
                    "host": "127.0.0.1",
                    "port": self.imap.port,
                    "tls_mode": "none",
                    "username": "probe",
                    "password": "secret",
                    "search_subject": "[nyxmon]",
                    "retries": 0,
                    "timeout": 5,
                },
            ),
            Check(
                15, "ping", "ping", "", {"host": "192.0.2.15", "count": 1, "timeout": 1}
            ),
            Check(
                16, "unknown type", "legacy", "http://127.0.0.1:1/", {}, ends_ok=False
            ),
            Check(
                17,
                "configuration error",
                "http",
                self._http(17, [ok()]),
                {"timeout": "soon"},
                ends_ok=False,
            ),
            Check(18, "disabled", "http", self._http(18, [ok()]), {}, disabled=True),
        ]
        if self.dns is not None:
            name = f"c12.{self.label}.parity.test"
            self.dns.script.set(
                name,
                [
                    {"a": ["10.0.0.1"]},
                    {"a": ["10.0.0.2"]},
                    "nxdomain",
                    {"a": ["10.0.0.1"]},
                ],
            )
            checks.append(
                Check(
                    12,
                    "dns",
                    "dns",
                    name,
                    {
                        "expected_ips": ["10.0.0.1"],
                        "dns_server": self.dns.address,
                        "timeout": 2,
                    },
                )
            )
        (self.ping_state / "scripts.json").write_text(
            json.dumps({"192.0.2.15": ["ok", "unreachable", "unreachable", "ok"]})
        )
        return sorted(checks, key=lambda check: check.check_id)

    def tokens(self) -> list[tuple[str, str]]:
        """Agent-specific strings and their neutral placeholders."""
        pairs = [(f"127.0.0.1:{self.http.port}", "127.0.0.1:<http>")]
        pairs += [
            (f"127.0.0.1:{fake.port}", f"127.0.0.1:<tcp{cid}>")
            for cid, fake in self.tcp.items()
        ]
        pairs += [(f".{self.label}.parity.test", ".<agent>.parity.test")]
        return pairs

    def ports(self) -> dict[int, str]:
        ports = {
            self.http.port: "<http>",
            self.smtp.port: "<smtp>",
            self.imap.port: "<imap>",
        }
        ports.update({fake.port: f"<tcp{cid}>" for cid, fake in self.tcp.items()})
        return ports

    def close(self) -> None:
        self.http.close()
        self.smtp.close()
        self.imap.close()
        for fake in self.tcp.values():
            fake.close()


def seed(db: Path, checks: list[Check]) -> None:
    connection = sqlite3.connect(db)
    connection.executescript(SCHEMA_SQL.read_text())
    connection.execute("INSERT INTO service (id, name) VALUES (1, 'parity')")
    for check in checks:
        connection.execute(
            "INSERT INTO health_check (id, service_id, name, check_type, url, check_interval,"
            " next_check_time, processing_started_at, status, disabled, data)"
            " VALUES (?, 1, ?, ?, ?, ?, 0, 0, 'idle', ?, ?)",
            (
                check.check_id,
                check.name,
                check.check_type,
                check.url,
                INTERVAL,
                int(check.disabled),
                json.dumps(check.data),
            ),
        )
    connection.commit()
    connection.close()


# ------------------------------------------------------------------ polling


def read_only(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)


class Poller:
    """Records each check's notification state while it has exactly k
    results (200 ms polls; check intervals are 2 s)."""

    def __init__(self, db: Path, check_ids: list[int]) -> None:
        self.db = db
        self.check_ids = check_ids
        self.states: dict[int, dict[int, dict[str, int]]] = {
            cid: {} for cid in check_ids
        }
        self.counts: dict[int, int] = {cid: 0 for cid in check_ids}
        self.skipped: dict[int, list[int]] = {cid: [] for cid in check_ids}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _poll(self) -> None:
        connection = read_only(self.db)
        try:
            connection.execute("BEGIN")
            counts = dict(
                connection.execute(
                    "SELECT health_check_id, COUNT(*) FROM check_result GROUP BY health_check_id"
                ).fetchall()
            )
            rows = connection.execute(
                f"SELECT check_id, {', '.join(STATE_COLUMNS)} FROM check_notification_state"
            ).fetchall()
            connection.execute("COMMIT")
        finally:
            connection.close()
        states = {row[0]: dict(zip(STATE_COLUMNS, row[1:])) for row in rows}
        for cid in self.check_ids:
            count = counts.get(cid, 0)
            previous = self.counts[cid]
            if count > previous + 1:
                self.skipped[cid].extend(range(previous + 1, count))
            self.counts[cid] = count
            if count and count not in self.states[cid]:
                self.states[cid][count] = states.get(cid, {})

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except sqlite3.OperationalError:
                pass  # locked or not yet created; the next poll retries
            self._stop.wait(0.2)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()


# ---------------------------------------------------------------- comparison


def normalise(value: Any, agent: Agent, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: normalise(v, agent, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalise(v, agent, key) for v in value]
    if isinstance(value, str):
        for token, placeholder in agent.tokens():
            value = value.replace(token, placeholder)
        return value
    if key == "port" and isinstance(value, int) and not isinstance(value, bool):
        return agent.ports().get(value, value)
    return value


def mask(value: Any, check_type: str, key: str = "") -> Any:
    if isinstance(value, dict):
        error_type = value.get("error_type")
        masked = {}
        for k, v in value.items():
            if k in MASKED_KEYS or k.endswith(MASKED_SUFFIXES):
                masked[k] = "<masked>"
            elif k == "error_msg" and (check_type, error_type) in P2_ERROR_MSG:
                masked[k] = "<P2>"
            else:
                masked[k] = mask(v, check_type, k)
        return masked
    if isinstance(value, list):
        return [mask(v, check_type, key) for v in value]
    return value


def results(db: Path, check_id: int) -> list[tuple[str, Any]]:
    connection = read_only(db)
    try:
        rows = connection.execute(
            "SELECT status, data FROM check_result WHERE health_check_id = ? ORDER BY id",
            (check_id,),
        ).fetchall()
    finally:
        connection.close()
    return [(status, json.loads(data)) for status, data in rows]


def state_view(state: dict[str, int]) -> dict[str, Any]:
    return {k: (bool(v) if k in STATE_TIMESTAMPS else v) for k, v in state.items()}


PY_ALERT = re.compile(
    r" - nyxmon\.adapters\.notification - ERROR - Check failed: .* \(ID: (\d+)\), "
    r"Result: Result\(result_id=\w+, check_id=\d+ status=([\w.]+), data=(\{.*\})\)$"
)
RS_ALERT = re.compile(
    r" - nyxmon\.adapters\.notification - ERROR - notification check_id=(\d+) "
    r"status=(\w+) error_type=(.*)$"
)


def python_alerts(log: str) -> dict[int, list[tuple[str, str]]]:
    alerts: dict[int, list[tuple[str, str]]] = {}
    for line in log.splitlines():
        match = PY_ALERT.search(line)
        if match:
            status = match.group(2).rsplit(".", 1)[-1].lower()
            data = ast.literal_eval(match.group(3))
            alerts.setdefault(int(match.group(1)), []).append(
                (status, str(data.get("error_type", "")))
            )
    return alerts


def rust_alerts(log: str) -> dict[int, list[tuple[str, str]]]:
    alerts: dict[int, list[tuple[str, str]]] = {}
    for line in log.splitlines():
        match = RS_ALERT.search(line)
        if match:
            alerts.setdefault(int(match.group(1)), []).append(
                (match.group(2), match.group(3))
            )
    return alerts


def incident_view(key: str, alert_count: int, payload: str | None) -> dict[str, Any]:
    """A ``collector_incident`` row as compared: key, ``alert_count`` and the
    payload with its timestamps masked (counters, keys and order compared)."""
    try:
        decoded: Any = json.loads(payload or "{}")
    except ValueError:
        decoded = payload
    return {
        "key": key,
        "alert_count": alert_count,
        "payload": mask(decoded, "internal"),
    }


def incidents(db: Path) -> list[dict[str, Any]]:
    connection = read_only(db)
    try:
        rows = connection.execute(
            "SELECT incident_key, alert_count, payload FROM collector_incident ORDER BY incident_key"
        ).fetchall()
    finally:
        connection.close()
    return [incident_view(key, count, payload) for key, count, payload in rows]


def alert_differences(
    checks: list[Check],
    alerts_a: dict[int, list[tuple[str, str]]],
    alerts_b: dict[int, list[tuple[str, str]]],
) -> list[str]:
    """Per-check notification lists must match; disabled checks (which
    never run) must have none in either log."""
    differences = []
    disabled = {check.check_id for check in checks if check.disabled}
    ids = {check.check_id for check in checks} | set(alerts_a) | set(alerts_b)
    for cid in sorted(ids):
        listed_a, listed_b = alerts_a.get(cid, []), alerts_b.get(cid, [])
        if listed_a != listed_b:
            differences.append(
                f"check {cid}: notifications python={listed_a} rust={listed_b}"
            )
        elif cid in disabled and listed_a:
            differences.append(f"check {cid} (disabled): notifications {listed_a}")
    return differences


def compare(
    python: Agent,
    rust: Agent,
    db_a: Path,
    db_b: Path,
    poll_a: Poller,
    poll_b: Poller,
    log_a: str,
    log_b: str,
) -> list[str]:
    differences: list[str] = []
    alerts_a, alerts_b = python_alerts(log_a), rust_alerts(log_b)
    # The corpus is built to alert; no recognised line means the log format
    # changed and the notification comparison would be vacuous.
    for name, alerts in (("python", alerts_a), ("rust", alerts_b)):
        if not alerts:
            differences.append(f"no notification line recognised in the {name} log")
    for check in python.checks:
        cid = check.check_id
        rows_a, rows_b = results(db_a, cid), results(db_b, cid)
        if check.disabled:
            for name, db, rows in (("python", db_a, rows_a), ("rust", db_b, rows_b)):
                connection = read_only(db)
                status = connection.execute(
                    "SELECT status FROM health_check WHERE id = ?", (cid,)
                ).fetchone()[0]
                connection.close()
                if rows or status != "idle":
                    differences.append(
                        f"check {cid} (disabled): {name} has {len(rows)} results, status {status}"
                    )
            continue
        for k in range(SAMPLES):
            status_a, data_a = rows_a[k]
            status_b, data_b = rows_b[k]
            view_a = mask(normalise(data_a, python), check.check_type)
            view_b = mask(normalise(data_b, rust), check.check_type)
            if status_a != status_b:
                differences.append(
                    f"check {cid} sample {k + 1}: status python={status_a} rust={status_b}"
                )
            if list(view_a) != list(view_b):
                differences.append(
                    f"check {cid} sample {k + 1}: keys python={list(view_a)} rust={list(view_b)}"
                )
            if json.dumps(view_a) != json.dumps(view_b):
                differences.append(
                    f"check {cid} sample {k + 1}: data\n    python={json.dumps(view_a)}\n    rust  ={json.dumps(view_b)}"
                )
        for k in range(1, SAMPLES + 1):
            for name, poller in (("python", poll_a), ("rust", poll_b)):
                if k not in poller.states[cid]:
                    differences.append(
                        f"check {cid}: no state observed at sample {k} ({name}); harness timing"
                    )
            if k in poll_a.states[cid] and k in poll_b.states[cid]:
                state_a = state_view(poll_a.states[cid][k])
                state_b = state_view(poll_b.states[cid][k])
                if state_a != state_b:
                    differences.append(
                        f"check {cid} sample {k}: state python={state_a} rust={state_b}"
                    )
        if check.ends_ok:
            for name, db in (("python", db_a), ("rust", db_b)):
                connection = read_only(db)
                row = connection.execute(
                    "SELECT attempt_at FROM check_notification_state WHERE check_id = ?",
                    (cid,),
                ).fetchone()
                connection.close()
                if row and row[0]:
                    differences.append(
                        f"check {cid}: attempt_at not cleared after the final OK ({name})"
                    )
    differences += alert_differences(python.checks, alerts_a, alerts_b)
    if incidents(db_a) != incidents(db_b):
        differences.append(
            f"collector_incident python={incidents(db_a)} rust={incidents(db_b)}"
        )
    return differences


# -------------------------------------------------------------------- runner


PYTHON_AGENT = (
    "import sys; from nyxmon.entrypoints.cli import start_agent; "
    "sys.argv[0] = 'start-agent'; start_agent()"
)


def agent_env(ping_state: Path, fake_bin: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("NYXMON_", "TELEGRAM_", "OPSGATE_"))
        and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["NYXMON_PARITY_PING_STATE"] = str(ping_state)
    env["NYXMON_CHECK_BATCH_SIZE"] = "100"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def wait_for_samples(
    pollers: list[Poller],
    ids: list[int],
    processes: list[subprocess.Popen[bytes]],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if all(poller.counts[cid] >= SAMPLES for poller in pollers for cid in ids):
            return
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"an agent exited early with {process.returncode}")
        if time.monotonic() > deadline:
            missing = {
                str(poller.db.name): {
                    cid: poller.counts[cid]
                    for cid in ids
                    if poller.counts[cid] < SAMPLES
                }
                for poller in pollers
            }
            raise RuntimeError(f"timed out waiting for {SAMPLES} samples: {missing}")
        time.sleep(0.2)


def run(args: argparse.Namespace) -> int:
    rust_binary = Path(args.rust_binary).resolve()
    if not os.access(rust_binary, os.X_OK):
        print(f"not an executable: {rust_binary}", file=sys.stderr)
        return 2
    workdir = Path(tempfile.mkdtemp(prefix="nyxmon-parity-"))
    contexts = {
        "long": _openssl_cert(workdir, "long", 400),
        "short": _openssl_cert(workdir, "short", 10),
    }
    dns: DnsFake | None = None
    try:
        dns = DnsFake(args.dns_address)
    except OSError as exc:
        if args.require_dns:
            print(
                f"cannot bind {args.dns_address}:53 for the DNS checks: {exc}",
                file=sys.stderr,
            )
            return 2
        print(f"NOTICE: DNS checks left out, cannot bind {args.dns_address}:53 ({exc})")
    fake_bin = workdir / "bin"
    fake_bin.mkdir()
    ping = fake_bin / "ping"
    ping.write_text(FAKE_PING.replace("{python}", sys.executable))
    ping.chmod(0o755)

    python, rust = (
        Agent("a", workdir, contexts, dns),
        Agent("b", workdir, contexts, dns),
    )
    db_a, db_b = workdir / "python.sqlite3", workdir / "rust.sqlite3"
    seed(db_a, python.checks)
    seed(db_b, rust.checks)
    enabled = [check.check_id for check in python.checks if not check.disabled]
    common = ["--interval", "1", "--disable-cleaner"]
    log_a_path, log_b_path = workdir / "python.log", workdir / "rust.log"
    with open(log_a_path, "wb") as log_a, open(log_b_path, "wb") as log_b:
        process_a = subprocess.Popen(
            [sys.executable, "-c", PYTHON_AGENT, "--db", str(db_a), *common],
            env=agent_env(python.ping_state, fake_bin),
            stdout=log_a,
            stderr=subprocess.STDOUT,
            cwd=workdir,
        )
        process_b = subprocess.Popen(
            [str(rust_binary), "--db", str(db_b), *common, "--shadow"],
            env=agent_env(rust.ping_state, fake_bin),
            stdout=log_b,
            stderr=subprocess.STDOUT,
            cwd=workdir,
        )
        poll_a, poll_b = Poller(db_a, enabled), Poller(db_b, enabled)
        failure = None
        try:
            wait_for_samples(
                [poll_a, poll_b], enabled, [process_a, process_b], args.timeout
            )
        except RuntimeError as exc:
            failure = str(exc)
        finally:
            for process in (process_a, process_b):
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
            codes = [process.wait(timeout=30) for process in (process_a, process_b)]
            poll_a.stop()
            poll_b.stop()
            python.close()
            rust.close()
            if dns is not None:
                dns.close()
    log_a_text, log_b_text = log_a_path.read_text(), log_b_path.read_text()
    if failure is None and codes != [0, 0]:
        failure = f"agent exit codes python={codes[0]} rust={codes[1]}"
    if failure is not None:
        print(
            f"FAILED: {failure}\n--- python log (tail)\n{log_a_text[-4000:]}\n--- rust log (tail)\n{log_b_text[-4000:]}"
        )
        return 1
    differences = compare(
        python, rust, db_a, db_b, poll_a, poll_b, log_a_text, log_b_text
    )
    checked = len(enabled)
    if args.keep:
        print(f"work directory kept: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    if differences:
        print(
            f"{len(differences)} unmasked difference(s) over {checked} checks x {SAMPLES} samples:"
        )
        for difference in differences:
            print(f"- {difference}")
        return 1
    print(
        f"parity OK: {checked} enabled checks x {SAMPLES} samples, "
        f"{sum(len(v) for v in python_alerts(log_a_text).values())} notifications, "
        f"{len(python.checks) - checked} disabled check(s); zero unmasked differences"
        + ("" if dns is not None else " (DNS checks left out)")
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rust-binary", required=True, help="path of the nyxmon-agent binary"
    )
    parser.add_argument(
        "--dns-address",
        default="127.0.0.1",
        help="address of the fake nameserver (port 53)",
    )
    parser.add_argument(
        "--require-dns", action="store_true", help="fail when port 53 cannot be bound"
    )
    parser.add_argument(
        "--timeout", type=float, default=90.0, help="seconds to wait for every sample"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the work directory (databases, logs)"
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
