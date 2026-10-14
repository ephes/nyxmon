# Rust Monitoring Agent (`nyxmon-agent`)

`nyxmon-agent` is a Rust port of the monitoring agent (`start-agent`). It
runs the same checks against the same SQLite database, writes the same rows,
sends the same Telegram and OpsGate messages and takes the same command line
and environment variables, so either implementation can run the
`nyxmon-monitor` service and the dashboard cannot tell which one wrote a row.
The Python agent stays in the repository as the reference implementation and
the rollback target.

The contract is `agent-rs/PLAN.md`; build, test and release commands are in
`agent-rs/README.md`. This page is the overview for operators and
contributors.

```{note}
Nothing deploys the Rust agent until the ops-library `nyxmon_deploy` role is
switched to it (`nyxmon_agent_impl: rust`, PLAN.md 8.3). Before the switch
the binary is validated by the process tests, the offline parity harness and
a shadow run on the production host (below).
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
| Telegram (MarkdownV2 with plain-text fallback), OpsGate tickets, the logging/shadow notifier | `nyxmon-notify` | `adapters/notification.py` |
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
and refuses to start otherwise (exit 1 with the missing columns). The Python
agent still creates and upgrades its tables itself (`_ensure_schema`), but
**new DDL must never be added there**: a schema change is a Django migration,
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
  Python code generates (`agent-rs/fixtures`, a drift test fails when Python
  changes without regenerating them).
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
| `nyxmon-agent --db PATH --shadow` | shadow run on a database copy: the logging notifier with one machine-readable line per alert (`notification check_id=<id> status=<s> error_type=<t>`); `TELEGRAM_*`/`OPSGATE_*` are ignored and `--enable-telegram` is refused |
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
own lock, which is what makes a shadow run possible.

### Shutdown

SIGTERM and SIGINT stop new claims, give a running batch
`NYXMON_SHUTDOWN_GRACE_SECONDS` (default 5) to finish, record the results
that finished in time, abort the rest and release their claims (idle again,
next run one check interval later), so they never turn into stale-lease
results. After SIGKILL the
claims stay until the processing lease expires; the next start then records
one `stale_processing_lease` result per check and sends one batch alert,
exactly like the Python agent.

### Shadow run

Before the switch, the binary runs next to the production agent on a copy
of the database (PLAN.md 9.1), with no Telegram and no writes to the
production database. The tools are in `agent-rs/parity/` and need only the
host's `python3`. Deployments do not ship `agent-rs/`, so copy them to the
host first, into a directory only the `nyxmon` user can read, and run them as
that user (it owns the live database):

```bash
install -d -o nyxmon -g nyxmon -m 0700 /var/lib/nyxmon-shadow /var/lib/nyxmon-shadow/tools
install -o nyxmon -g nyxmon -m 0600 shadow_prepare.py shadow_record.py \
    shadow_compare.py /var/lib/nyxmon-shadow/tools/
```

1. **Copy and prepare**:

   ```bash
   sudo -u nyxmon python3 /var/lib/nyxmon-shadow/tools/shadow_prepare.py \
       --source /home/nyxmon/site/db.sqlite3 \
       --dest /var/lib/nyxmon-shadow/db.sqlite3
   ```

   It copies the live database online (SQLite backup API), disables the
   `smtp` and `imap` checks in the copy (a shadow IMAP check would delete the
   messages the production check expects; a shadow SMTP check sends mail),
   releases the claims the copy inherited, deletes the copy's collector
   incidents, prints T0 and writes the notification-state baseline B0 to
   `db.sqlite3.baseline.json`.

2. **Run the shadow agent** in tmux, with the production hardening and an
   environment file holding only `NYXMON_*` variables:

   ```bash
   systemd-run --unit nyxmon-agent-shadow -p User=nyxmon -p Group=nyxmon \
       -p NoNewPrivileges=yes -p ProtectSystem=strict \
       -p ReadWritePaths=/var/lib/nyxmon-shadow \
       -p EnvironmentFile=/var/lib/nyxmon-shadow/shadow.env \
       /opt/nyxmon-agent/<version>/nyxmon-agent \
       --db /var/lib/nyxmon-shadow/db.sqlite3 --shadow
   journalctl -fu nyxmon-agent-shadow
   ```

3. **Record** both databases every 60 s in the same tmux session (read-only;
   new results, changed notification states and incidents, lease snapshots,
   retention counters, and memory/CPU of both units):

   ```bash
   sudo -u nyxmon python3 /var/lib/nyxmon-shadow/tools/shadow_record.py \
       --prod /home/nyxmon/site/db.sqlite3 \
       --shadow /var/lib/nyxmon-shadow/db.sqlite3 --since <T0> \
       --unit nyxmon-monitor --unit nyxmon-agent-shadow \
       --out /var/lib/nyxmon-shadow/record.jsonl
   ```

4. **Compare** after the window (24 h for phase A):

   ```bash
   journalctl -u nyxmon-agent-shadow --since @<T0> -o short-unix \
       > /var/lib/nyxmon-shadow/shadow.log
   sudo -u nyxmon python3 /var/lib/nyxmon-shadow/tools/shadow_compare.py \
       --record /var/lib/nyxmon-shadow/record.jsonl --since <T0> \
       --baseline /var/lib/nyxmon-shadow/db.sqlite3.baseline.json \
       --shadow-log /var/lib/nyxmon-shadow/shadow.log \
       --prod-db /home/nyxmon/site/db.sqlite3 \
       --shadow-db /var/lib/nyxmon-shadow/db.sqlite3 \
       --json /var/lib/nyxmon-shadow/report.json
   ```

   Export the journal before the transient unit is stopped; `short-unix`
   timestamps let the comparison count only the alert lines inside the
   recorded window (earlier runs of the same unit name are left out). The
   report lists per check the outcomes of both agents, every shadow
   sample whose status or error type differs from the nearest production
   sample (with both timestamps, so a changed endpoint explains it), data
   key lists that occur in only one database, the number of notification
   decisions per database (`attempt_seq` now minus `attempt_seq` in B0,
   cross-checked with the shadow agent's notification lines), stale-lease
   results, claims stuck in `processing`, whether the shadow cleaner deletes old results, and memory
   and CPU against the budgets of PLAN.md 4.4. It ends with the findings to
   explain; acceptance is "every finding explained".

5. **Phase B** repeats the run with `NYXMON_SITE_CONNECTIVITY_MODE=observe`;
   **phase C** runs the mail checks once from the copy:
   `nyxmon-agent run-check --db /var/lib/nyxmon-shadow/db.sqlite3 --check-id <smtp id>`
   and `run-check --imap-no-delete --check-id <imap id>`, compared with the
   latest production results of the same checks.

Stop the unit (`systemctl stop nyxmon-agent-shadow`) and remove
`/var/lib/nyxmon-shadow` afterwards.

### Switching and rollback

The `nyxmon-monitor` unit runs one implementation at a time, chosen by the
deployment role (`nyxmon_agent_impl: python | rust` in the ops-control host
variables, PLAN.md 8.3-8.5). Rolling back is flipping the variable to
`python` and deploying; in an emergency, remove the drop-in
`/etc/systemd/system/nyxmon-monitor.service.d/agent-impl.conf`, run
`systemctl daemon-reload` and restart the unit. Nothing in the database
depends on which agent wrote it, and the lock makes sure the two never run
on the production database at once.

## Testing

- **Fixtures** (`just rs-fixtures`): the Python code generates the expected
  outputs of every pure function and of executor runs against scripted
  servers; the Rust tests replay them.
- **Process tests** (`agent-rs/crates/nyxmon-agent/tests/binary.rs`): the
  built binary against temporary databases and fake servers (signals,
  SIGKILL and stale-lease reclaim, the cleaner, the site observer, the lock,
  `run-check`).
- **Offline parity harness** (`just rs-parity`, CI job `parity`): both agents
  side by side on one corpus of checks against scripted fake servers; every
  difference in results, key order, notification state or alerts outside
  the documented masks fails the run (PLAN.md 7.4).
- **Shadow run** on the production host before the switch (above).
