# Rust Monitoring Agent (`nyxmon-agent`)

`nyxmon-agent` is a Rust port of the monitoring agent (`start-agent`). It
runs the same checks against the same SQLite database, writes the same rows,
sends the same Telegram and OpsGate messages and takes the same command line
and environment variables, so it replaced the Python agent in the
`nyxmon-monitor` service without the dashboard noticing which one wrote a row.
The Python agent (`start-agent` in `src/nyxmon`) stays in the repository, but
it is no longer deployed and no longer a rollback target.

The contract is `agent-rs/PLAN.md`; build, test and release commands are in
`agent-rs/README.md`. This page is the overview for operators and
contributors.

```{note}
Production runs the Rust agent since 2026-10-16 (cutover with release
`agent-rs-v0.1.0`; macmini runs `agent-rs-v0.1.1` since 2026-10-21). Before
the switch the binary was validated by the process tests, an offline parity
harness and a shadow run on the production host. After a clean 7-day soak the
Python fallback was retired on 2026-10-25: the deployment no longer offers a
switch back to `start-agent`, and the parity harness, the shadow-run tools
and the fixture drift test were removed (see git history). Rollback is a
previous Rust release pin, or git history. The `start-agent` entry point
remains in `src/nyxmon` but is not deployed; removing it together with its
`_ensure_schema` DDL is a separate follow-up.
```

## Architecture

The binary is one process with one tokio runtime (two worker threads, up to
32 blocking threads for the system resolver) and one dedicated thread that
owns the only read-write SQLite connection (the "DB actor"):

| Component | Crate | Python counterpart |
| --- | --- | --- |
| JSON values with CPython semantics (`PyJson`), environment readers, notification policy, suppression rules, the notification decision, the site observer state machine, lease estimate | `nyxmon-core` | `domain/`, `service_layer/`, `site_connectivity.py` (pure parts) |
| SQLite store: claims, stale reclaim, release, result persistence, incidents, cleaner deletes, schema verification, the agent lock | `nyxmon-store` | `adapters/repositories/sqlite_repo.py`, `agent_lock.py` |
| Check executors: `http`, `json-http`, `json-metrics`, `tcp`, `ping`, `dns`, `smtp`, `imap`; maintenance-suppression fetch | `nyxmon-checks` | `adapters/runner/executors/` |
| Telegram (MarkdownV2 with plain-text fallback), OpsGate tickets, the logging notifier (`--shadow`) | `nyxmon-notify` | `adapters/notification.py` |
| Collector loop, batch runner, result handler, cleaner, site observer runtime, CLI and shutdown | `nyxmon-agent` | `collector.py`, `async_runner.py`, `handlers.py`, `cleaner.py`, `cli.py` |

Long-lived tasks: the collector loop (one batch task per iteration), the
cleaner loop (unless `--disable-cleaner`), the site observer (only when
`NYXMON_SITE_CONNECTIVITY_MODE` is not `off`) and the main task that handles
signals. Every store operation is one transaction; writes start with
`BEGIN IMMEDIATE`. Checks of a batch run concurrently and their results are
handled one at a time in completion order (suppression, decision,
persistence, notification, acknowledgement), as in Python. See
{doc}`concurrency-overview` for both runtimes.

HTTP checks use a small HTTP/1.1 client that reproduces httpx (URL handling,
per-read timeouts, redirects, cookies, decoding); TLS is rustls with the
system roots (TLS 1.2+). The binary is built for `x86_64-unknown-linux-gnu`
against glibc 2.35 or older, so names resolve through `getaddrinfo` and
`nsswitch.conf` exactly like Python (PLAN.md 8.1).

## Schema contract

Django migrations own the database schema. The Rust agent runs no DDL: at
startup it checks that every table and column it uses exists and that the
`nyxboard` migration it needs (`0013_site_connectivity_state`) is applied,
and refuses to start otherwise (exit 1 with the missing columns). The
undeployed Python agent still creates and upgrades its tables itself
(`_ensure_schema`, to be removed with it), but **new DDL must never be added
there**: a schema change is a Django migration,
and `just rs-fixtures` regenerates the schema contract the Rust tests and
`tests/unit/test_agent_rs_schema_contract.py` check. A migration that the
Rust agent needs raises its minimum migration and is a breaking agent
release.

`nyxmon-agent --db PATH --check-schema` runs the same verification
read-only, without the lock, and exits 0 or 1; the deployment role runs it
before it points the service at a new binary.

## What "same behaviour" means

PLAN.md 1.3 defines three parity classes:

- **P1 — exact**: every SQL effect (rows, columns, values, transaction
  boundaries), `check_result.status` and the keys, key order and values of
  `check_result.data` (serialised like Python's `json.dumps`), notification
  decisions and `check_notification_state` transitions, collector incident
  payloads, the text of every Telegram, OpsGate and collector message,
  environment variables, CLI flags, exit codes, signal handling, and the
  operator log lines listed in PLAN.md 1.7. Pinned by fixtures that the
  Python code generated (`agent-rs/fixtures`). Since the Python fallback was
  retired (2026-10-25) the fixtures are Rust-owned test data: the generators
  and the drift test are gone, and a behaviour change edits the fixture
  together with the Rust code.
- **P2 — equivalent**: error messages that come from library exceptions
  (`str(exc)` of httpx, ssl, smtplib, dnspython), durations, other log lines,
  and the order of set reprs in configuration errors.
- **P3 — intentional divergences**, listed exhaustively in PLAN.md 1.5. The
  owner's parity policy: byte parity for everything that occurs in
  production configurations and network responses; for exotic inputs that
  do not, the Rust agent may fail closed (a `configuration_error` or a clear
  error result) instead of reproducing CPython internals, and each such case
  is documented and pinned by a test.

### Divergences (PLAN.md 1.5)

| | Divergence |
| --- | --- |
| D-2 | Set reprs in configuration error messages are sorted. |
| D-3 | At shutdown an in-flight batch is aborted before leases are released, so no result is written after the release (Python could still persist one). |
| D-4 | An executor panic is `executor_exception` with message `panic`. |
| D-5 | No DDL; an old schema refuses to start (see above). |
| D-6 | Only ASCII digits count as digits (environment values, JSON paths, numeric strings). |
| D-8 | JSON with lone UTF-16 surrogates (and `data` the store cannot decode) is rejected as a whole; such a check reports `configuration_error`. |
| D-9 | rustls instead of OpenSSL: no TLS 1.0/1.1, no SHA-1 signatures, no RSA keys under 2048 bits; server names rustls cannot represent are sent without SNI and fail verification closed. |
| D-10 | The agent lock file `<db>.agent.lock` (also taken by the Python agent). |
| D-11 | `health_check.data` that is JSON `null` reads as `{}`; other non-objects are "no configuration" for policy, suppression and site dependency. |
| D-12 | Random values (SMTP correlation token, OpsGate boundary nonce) differ by nature. |
| D-13, D-15 | Proxy environment variables are not honoured (neither by the notifiers nor by HTTP checks and the suppression fetch); the production unit sets none. |
| D-14 | SMTP `from_addr`/`to_addr` must be plain ASCII addresses (optionally with a display name); anything else is a `configuration_error`. |
| D-16 | DNS: non-ASCII names, DNS-over-HTTPS nameservers in `resolv.conf` and some malformed responses are handled differently from dnspython. |
| D-17 | Non-UTF-8 `PATH` entries are skipped when looking up `ping`. |
| D-18 | IMAP: non-string hosts and out-of-range or unparsable INTERNALDATEs. |
| D-19 | A persisted site payload that cannot be restored stops the observer with a log line. |
| D-20 | Processing leases beyond SQLite's integer range. |

D-1 and D-7 were withdrawn: non-standard JSON numbers and big integers are
handled exactly like CPython.

## Operations

### Command line

`nyxmon-agent` accepts `start-agent`'s flags with the same defaults
(`--db`, `--interval`, `--cleanup-interval`, `--retention-period`,
`--batch-size`, `--disable-cleaner`, `--log-level`, `--enable-telegram`) and
reads the same environment variables ({doc}`configuration`). Additions:

| Command | Purpose |
| --- | --- |
| `nyxmon-agent --version` | `nyxmon-agent <version> (<git commit>)` |
| `nyxmon-agent --db PATH --check-schema` | verify the schema read-only, exit 0/1 |
| `nyxmon-agent --db PATH --shadow` | the logging notifier with one machine-readable line per alert (`notification check_id=<id> status=<s> error_type=<t>`) instead of Telegram/OpsGate; `TELEGRAM_*`/`OPSGATE_*` are ignored and `--enable-telegram` is refused. Only the notifier changes: checks run, results and notification state are written and the cleaner deletes as in production, so use it only on a copy of the database, and disable the `smtp` and `imap` checks in the copy first (an SMTP check sends real mail; an IMAP check deletes the messages it matched, which the production check expects). |
| `nyxmon-agent run-check --db PATH --check-id N [--imap-no-delete]` | execute one check from a read-only database and print the result as one JSON line; no lock, no result row, no notification; disabled checks run too |

Log lines go to stderr in the Python format
(`%(asctime)s - %(name)s - %(levelname)s - %(message)s`, the Python logger
names). Exit status: 0 after SIGTERM/SIGINT, 1 for a missing database, an old
schema, a held agent lock or a fatal database error, 2 for usage errors.

### One agent per database

Both implementations take an exclusive `flock` on `<db path>.agent.lock`
before they touch the database; a second agent exits 1 with
`another nyxmon agent holds <lock path>; refusing to start` (see "Operations:
one agent per database" in {doc}`configuration`). A database copy has its
own lock, so a `--shadow` run on a copy does not conflict with the
production agent.

### Shutdown

SIGTERM and SIGINT stop new claims, give a running batch
`NYXMON_SHUTDOWN_GRACE_SECONDS` (default 5) to finish, record the results
that finished in time, abort the rest and release their claims (idle again,
next run one check interval later), so they never turn into stale-lease
results. After SIGKILL the
claims stay until the processing lease expires; the next start then records
one `stale_processing_lease` result per check and sends one batch alert,
exactly like the Python agent.

### Deployment and rollback

The `nyxmon-monitor` unit runs the pinned `nyxmon-agent` release
(`/opt/nyxmon-agent/<version>/nyxmon-agent`); the ops-library `nyxmon_deploy`
role and its pyinfra port install, validate (`--version`, `--check-schema`)
and select it. Rollback is pinning the previous Rust release again (the
previously selected version stays installed) and deploying, or a revert
from git history. Until 2026-10-25 the role could also switch back to the
Python agent (`nyxmon_agent_impl: python`); that fallback is retired.

The cutover on 2026-10-16 was verified as in PLAN.md 9.3: every enabled check
had a new result within its interval, the status distribution matched the
hour before, no claim got stuck, one controlled failing check sent exactly
one Telegram message and opened one OpsGate ticket, and a rollback drill
(Rust to Python and back, two deploys) showed each agent continuing on the
database the other left. Memory against the budget of PLAN.md 4.4: cgroup
memory about 12 MB (peak 14.4 MB) and anonymous RSS about 10.7 MiB; `ps`
reports about 21 MiB RSS, of which about 10.3 MiB is the file-backed mapping
of the binary. The ops-control runbook `docs/NYXMON_RUST_AGENT.md` has the
commands and the deploy log.

## Testing

- **Fixtures** (`agent-rs/fixtures`): expected outputs of every pure function
  and of executor runs against scripted servers, generated from the Python
  code until 2026-10-25 and Rust-owned test data since; the Rust tests replay
  them. `just rs-fixtures` regenerates only the schema contract
  (`fixtures/schema/`) from the Django migrations.
- **Process tests** (`agent-rs/crates/nyxmon-agent/tests/binary.rs`): the
  built binary against temporary databases and fake servers (signals,
  SIGKILL and stale-lease reclaim, the cleaner, the site observer, the lock,
  `run-check`).
- **Before the cutover** an offline parity harness (both agents side by side
  against scripted fake servers, PLAN.md 7.4) and a shadow run on the
  production host (PLAN.md 9.1) compared the two implementations. Both were
  removed with the Python fallback on 2026-10-25; see git history.
