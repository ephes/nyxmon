# nyxmon-agent (Rust)

A Rust port of the nyxmon monitoring agent (`start-agent`). The contract is
[`PLAN.md`](PLAN.md); the Python agent in `src/nyxmon` stays the reference
implementation and the rollback target. **Nothing deploys this code yet**:
rsync and git deploys ship `src/` only, and the ops-library role ignores
`agent-rs/` until slice S14.

Status: slice S1 (foundations): workspace, CI, recipes, the `pyjson`
module, the fixture pipeline, the environment readers, the domain types and
the TLS helpers; slice S3 (alerting core): notification policy, site
dependencies, the site snapshot hold logic, maintenance-suppression rules,
the notification decision, the runtime (lease) estimate and the incident
delivery markers, all pure functions in `nyxmon-core`; slice S4 (site
observer core): the site connectivity observer as a pure state machine
(`nyxmon_core::site::Observer`: path and phase machines, payload codec and
restore, ongoing alert, summaries, recovery recheck, message rendering) whose
I/O goes through the `SitePorts` trait; slice S2 (in progress): the SQLite
store (`nyxmon-store`: DB actor, claims and holds) and the schema contract;
result persistence, incidents, the cleaner batch and the agent lock follow.
The binary only answers `--version`.

Slice S8 adds the notifiers (`nyxmon-notify`): Telegram with the OpsGate
ticket integration, and the logging/shadow notifier.

## Layout

```
agent-rs/
  Cargo.toml            workspace; release profile (thin LTO, stripped)
  Cargo.lock            committed; every build uses --locked
  rust-toolchain.toml   pinned toolchain (also the MSRV)
  PLAN.md               the implementation plan and parity contract
  TESTMAP.md            Python test -> Rust test coverage, per slice
  crates/
    nyxmon-core/        pure logic (no I/O): pyjson, env readers, domain types,
                        alerting (policy, site, suppression, decision, lease)
    nyxmon-store/       SQLite store: DB actor, all SQL, schema contract
    nyxmon-checks/      executors (S5-S7); TLS helpers (S1)
    nyxmon-notify/      Telegram, OpsGate, logging notifier (S8, done)
    nyxmon-agent/       the binary (S9+)
  fixtures/
    generate.py         writes the fixtures from the Python code
    gen_*.py            per-slice fixture builders loaded by generate.py
    generate_schema.py  writes schema/ from `manage.py migrate`
    *.json              generated fixtures (do not edit by hand)
    schema/             the Django schema contract (generated)
```

## Requirements

- [rustup](https://rustup.rs); it installs the toolchain pinned in
  `rust-toolchain.toml` on first use. Run cargo from inside `agent-rs/` (the
  just recipes do) so the pin applies; a non-rustup `cargo` on `PATH` ignores
  the pin.
- `uv` for the fixture generator (`uv sync` at the repository root).
- Docker only for `just rs-build-linux`; on an ARM Mac the Docker VM needs
  amd64 emulation (Docker Desktop's default; colima: `--vz-rosetta` or
  QEMU binfmt), otherwise the container fails with `exec format error`.

## Commands

From the repository root:

| Command | What it does |
| --- | --- |
| `just rs-test` | `cargo test --workspace --locked` |
| `just rs-lint` | `cargo fmt --all --check` and `cargo clippy --workspace --all-targets --locked -- -D warnings` |
| `just rs-fmt` | format the workspace |
| `just rs-fixtures` | regenerate `fixtures/*.json` from the Python code |
| `just rs-build` | release build for the host (`agent-rs/target/release/nyxmon-agent`) |
| `just rs-build-linux` | release build for macmini (`x86_64-unknown-linux-gnu`, glibc) in a pinned `rust:<ver>-bookworm` container; output under `agent-rs/target/docker-linux-amd64/` |
| `just test` / `just lint` / `just typecheck` | the Python checks followed by `rs-test` / `rs-lint` / `cargo check --workspace --all-targets --locked` |

CI runs the same Rust checks in the `rust` job of `.github/workflows/ci.yml`
and reports the release binary size; the fixture drift test runs in the
`python` job as part of pytest.

## Fixtures: Python is the oracle

`fixtures/generate.py` calls the real Python functions on a fixed corpus
(clocks, nonces and the environment injected) and writes deterministic JSON
(`sort_keys`, `indent=1`, ASCII). Rust tests embed the files with
`include_str!` and must reproduce every case. `tests/unit/test_agent_rs_fixtures.py`
regenerates them in memory and fails when a committed file differs, so a
Python behaviour change cannot merge without `just rs-fixtures`, and the Rust
tests then show what the port must change. Each slice adds its builders to
`GENERATORS` in `generate.py`.

The oracle is the newest CPython 3.13 patch release (what CI's
`setup-python` installs; `uv python install 3.13` locally). Older patch
releases can differ in corner cases, which the drift test then reports.

Current fixtures:

- `pyjson.json`: `json.loads`/`json.dumps` (every option set the agent uses),
  float `repr`, `format(x, "g")`, `round`, `int()`/`float()` parsing, rich
  comparison over JSON values, truthiness, `repr(str)`/`repr(bytes)`.
- `env.json`: every environment reader of PLAN.md section 1.4 with its
  warnings, including warn-once sequences.
- `idna.json`: `host.encode("idna")`, the server name Python's `ssl` sends
  for a Unicode host (`nyxmon_checks::tls::idna_encode`).
- `idna_tables.json`: the Unicode 3.2 stringprep tables of Python's
  `nameprep`, embedded by `nyxmon_checks::pyidna` so the IDNA 2003 encoding
  is Python's.
- `config_tcp.json`: the TCP check configuration (`TcpCheckConfig.from_dict`
  + `validate`), the target host (`urlparse(url).hostname`), `str()` of
  non-string hosts, `ip_address(int)` and anyio's IDNA 2008 encoding of
  hosts (`nyxmon_core::config::tcp`, `nyxmon_checks::idna2008`).
- `idna2008_tables.json`: the data behind `idna.encode(host, uts46=True)`:
  the `idna` package's UTS 46 table, code point classes, joining types and
  scripts, and CPython's bidirectional classes, marks and virama class,
  embedded by `nyxmon_checks::idna2008`.
- `starttls_text.json`: the STARTTLS classification helpers and patterns,
  and scripted dialogues (received chunks, sent bytes, outcome) replayed
  through `nyxmon_checks::starttls`.
- `unicode_nonprintable.json`: the code points `str.isprintable()` rejects.
  `nyxmon-core` embeds it, so `repr(str)` escapes exactly what the oracle's
  Unicode database (15.1 for CPython 3.13) escapes.
- `policy.json`, `site_dependency.json`, `site_snapshot.json`,
  `suppression_rules.json`, `decision.json`, `lease.json`,
  `incident_payloads.json` (`gen_alerting.py`): the alerting core. JSON
  whose key order matters is stored as JSON text (`*_json`) because the
  files are written with sorted keys. `decision.json` is generated under
  `sys.monitoring` line tracing and the generator fails unless the corpus
  executes every line of `_should_notify_check_result`.
- `site_replay/*.json` (`gen_site.py`): the real `SiteConnectivityObserver`
  driven round by round by a scripted probe runner, clock, in-memory store
  and scheduler (with injected read/write/query failures) and a recording
  notifier. Each step records the store, scheduler and notifier calls and the
  log lines in call order (payloads as `json.dumps` text), the probed
  targets, the published snapshot and the exception a round raised;
  `nyxmon-core` replays them against `site::Observer` with the same fakes.
  `site_replay/coverage.json` fails generation unless the corpus executes
  every line of the observer's pure methods and rendering helpers.
- `unicode_props.json`: `str.isdigit()`, `str.lower()` (with the final-sigma
  properties) and the NFKC delimiter check of `urlsplit`, embedded by
  `nyxmon_core::pyjson` (`py_isdigit`, `py_lower`) so they follow the
  oracle's Unicode version.

## Schema contract

Django migrations own the schema (PLAN.md section 2). The Rust agent runs no
DDL: at startup it checks that every table and column it uses exists and that
`nyxboard.0013_site_connectivity_state` (`MIN_NYXBOARD_MIGRATION`) is
recorded, and refuses to start otherwise (`nyxmon_store::check_schema` is the
core of `--check-schema`). `fixtures/generate_schema.py` migrates a temporary
database with the test settings and writes `fixtures/schema/schema.sql` (DDL
plus `django_migrations` rows; every store test builds its database from it)
and `fixtures/schema/agent_tables.json` (the agent tables' columns and the
minimum migration). `tests/unit/test_agent_rs_schema_contract.py` fails when a
migration changes them: run `just rs-fixtures`, then update
`nyxmon-store/src/schema.rs` (and PLAN.md 2.2) if the agent tables changed.
Never add DDL to the Python agent; a new required column raises
`MIN_NYXBOARD_MIGRATION` and is a breaking agent release.
- `telegram.json` (S8): MarkdownV2 escaping and plain-text fallback, the
  alert text, and whole `async_notify_check_failed` calls replayed against
  scripted responses (`httpx.MockTransport`): request bodies, headers,
  timeouts, outcome and log lines.
- `opsgate.json` (S8): `sanitize_untrusted_data`, the remediation prompt,
  `task_ref`, and the ticket payload as httpx encodes it (nonce and clock
  injected).
- `unicode_untrusted.json` (S8): the code points of the categories Cc, Cf,
  Zl and Zp that the OpsGate sanitizer replaces by `?`; `nyxmon-notify`
  embeds it.

## Conventions for later slices

- JSON values are `nyxmon_core::PyJson`, never `serde_json::Value`: it keeps
  dict order, int/float distinction, big integers and `NaN` like CPython.
  Store and payload text is `PyJson::to_json_string()` (CPython's
  `json.dumps` defaults).
- `PyJson` parsing, serialisation, comparison, cloning and dropping are
  iterative, so hostile nesting (up to CPython's limit of 9998 levels) cannot
  overflow a thread stack. `PyJson` implements `Drop`: move contents out with
  `into_dict`/`into_list`/`into_string` instead of destructuring by value.
- Pure code reports operator-facing warnings through
  `nyxmon_core::log::LogSink` with the Python logger name; `TracingSink`
  forwards to `tracing` with that name as the target, `RecordingSink`
  captures them in tests.
- Warn-once state of the alerting readers (Python's module-level
  `_warned_*` sets) lives in one `nyxmon_core::log::WarnOnce` per process,
  keyed by `(scope, check_id, field)`.
- Environment readers take an `&Env` (`Env::process()` in the binary,
  `Env::from_map` in tests); its warn-once state lives as long as the `Env`.
  Unbounded integers are returned exactly as `BigInt` (see the `env`
  module documentation).
- The site observer is I/O-free: the driver (S11) implements
  `nyxmon_core::site::SitePorts` (clock, probe round, the
  `site:connectivity` row, scheduler queries, notifier, snapshot publication,
  log lines) and calls `Observer::restore`/`Observer::run_once`; an `Err`
  from either is the Python exception a malformed persisted value raises,
  which the loop logs as `site connectivity round failed`.
- Notifiers implement `nyxmon_notify::Notifier`
  (`notify_check_failed -> Result<Delivery, NotifyRaised>`); only
  `Delivery::Failed` is a delivery failure, and `NotifyRaised` stands for a
  Python exception escaping the notifier (the OpsGate expiry
  `OverflowError`), which the caller handles like any result-handling
  error. `TelegramNotifier::new` takes the `notifier_env` reading plus
  `NotifierSettings` (clock, nonce, log sink, Telegram endpoint for tests).
- Check configurations live in `nyxmon_core::config` and return a
  `ConfigOutcome` (`Ok`, `ConfigError(message)` for Python's `ValueError`,
  `PyException(class)` for exceptions that escape the executor and become
  `executor_exception`).
- TLS goes through `nyxmon_checks::tls` (rustls with the `ring` provider,
  system roots, TLS 1.2+); `tls::server_name` IDNA-encodes Unicode hosts
  like Python.
- Database access goes through `nyxmon_store::Store`, a cloneable handle to
  the one read-write connection on its own thread. Each method is one
  transaction (writes `BEGIN IMMEDIATE`); a sent operation completes even if
  the caller is cancelled. Where Python reads `time.time()` or
  `check_batch_size()` inside a repository method, the Rust method takes
  `now`/`batch_size` arguments. `StoreError::ActorDied` is fatal (exit 1).
- A `health_check.data` value that cannot be decoded reads as `PyJson::Null`
  (`nyxmon_store::data_is_undecodable`; valid `null` reads as `{}`, D-11).
  The runner reports such a check as `configuration_error` (D-8).
