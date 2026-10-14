# Rust port of the nyxmon monitoring agent — implementation plan

Status: plan, not yet implemented. Work item `nyxmon-rust-agent`.

This document is the contract for porting `start-agent`
(`src/nyxmon/entrypoints/cli.py:start_agent`, run by `nyxmon-monitor.service`
on macmini) to a Rust binary in `agent-rs/`. It is written for the
implementers of the slices in section 10 and for the reviewers of each slice.
Every behavioural statement cites the Python code that defines it; when this
document and the Python code disagree, the Python code wins and this document
must be corrected.

Facts about the live system were taken read-only on 2026-10-14 from macmini
(Ubuntu 24.04.5, x86_64, glibc 2.39, kernel 7.2, `/usr/bin/ping` from iputils
20240117 with `cap_net_raw=ep`, `net.ipv4.ping_group_range = 1 0`, no Rust
toolchain, SQLite 3.45.1 CLI, DB `/home/nyxmon/site/db.sqlite3` in
`journal_mode=delete`, nyxboard migrations applied up to
`0013_site_connectivity_state`, 76 checks: 44 json-metrics, 21 http, 4 dns,
3 tcp, 2 smtp, 2 imap, 0 ping; no `NYXMON_SITE_*` variables set, so site
connectivity is `off` in production; Python agent RSS 47 MiB, 7 threads;
unit `TimeoutStopSec` 90 s).

---

## 1. Scope, non-goals, parity definition

### 1.1 Scope

Replace the Python monitoring agent process with a Rust binary
`nyxmon-agent` that is a drop-in for `start-agent`:

- same CLI flags (`--db`, `--interval`, `--cleanup-interval`,
  `--retention-period`, `--batch-size`, `--disable-cleaner`, `--log-level`,
  `--enable-telegram`; `cli.py:start_agent`), same defaults, same environment
  variables (section 1.4);
- same reads and writes on the shared SQLite database
  (`health_check`, `check_result`, `check_notification_state`,
  `collector_incident`, `django_session`);
- all eight check types (`http`, `json-http`, `json-metrics`, `tcp`, `ping`,
  `dns`, `smtp`, `imap`);
- notification policy, maintenance suppression, per-check delivery retry and
  acknowledgement, Telegram (MarkdownV2 with plain-text fallback) and the
  OpsGate ticket integration (`adapters/notification.py`);
- collector semantics: bounded claims, processing leases, stale-lease reclaim,
  runtime-derived lease extension, wedged-batch abandonment and the
  execution-paused incident, collector incidents with delivery intent and
  acknowledgement (`adapters/collector.py`);
- the results cleaner including expired Django sessions
  (`adapters/cleaner.py`, `sqlite_repo.py:delete_old_results_async`);
- site connectivity observation, the `site:connectivity` incident row, holds,
  release watermarks, recovery recheck, ongoing alert and summaries
  (`adapters/site_connectivity.py`, `service_layer/site_dependency.py`,
  `handlers.py:_should_notify_check_result`);
- graceful shutdown that releases owned leases
  (`collector.py:AsyncCheckCollector._async_start`, `release_claims_async`);
- `ping` stays a subprocess of the system `ping` binary
  (`executors/ping_executor.py`).

The Python package stays in the repository. The Django dashboard
(`src/nyxboard`, `src/django`) is unchanged and keeps sharing `db.sqlite3`.
The Python agent remains runnable and is the rollback target.

### 1.2 Non-goals

- No change to the Django models, migrations, views or templates (beyond
  docs). The dashboard must not be able to tell which agent wrote a row.
- No port of `add-check`, `show-checks`, `llm-content`
  (`entrypoints/check_management.py`, `entrypoints/developer_tools.py`),
  `devdata.py`, the in-memory store, the message bus/unit-of-work/event
  plumbing (`service_layer/message_bus.py`, `unit_of_work.py`,
  `domain/events.py`): they are structure, not behaviour. The handlers that
  carry behaviour are ported as plain functions.
- `service_status_changed` / `ServiceStatusChanged` is never emitted by any
  production path and is not ported.
- `CheckResult.force_notification=True` is never set by a production path.
  The flag is kept in the pure decision function (section 5.3) so the shared
  fixtures cover it, but no Rust caller sets it.
- No Windows support (the Python ping executor has Windows branches; Rust
  implements `linux`, `darwin`/`freebsd` and "other" only, so the binary can
  be developed on macOS).
- No new features, no behaviour "fixes" except the intentional divergences in
  section 1.5. Latent Python quirks are reproduced and listed in section 11.
- No change of the SQLite journal mode (section 3.3).

### 1.3 What "same behaviour" means

Three parity classes. Every reviewer finding about parity must name the class.

**P1 — exact.** Must be identical, pinned by shared fixtures or tests:

- every SQL effect on the shared tables (which rows, which columns, which
  values, which transaction boundaries and guards), including
  `check_result.created_at` written by SQLite's `datetime('now')` (UTC,
  `YYYY-MM-DD HH:MM:SS`), and `id` assigned by SQLite (`AUTOINCREMENT` in the
  Django schema);
- `check_result.status` and the **key set, key order and values** of
  `check_result.data` for every nyxmon-authored field. Key order is
  user-visible: `healthcheck_detail.html` renders `{{ result.data }}`, i.e. the
  Python dict repr in insertion order. Rust uses the order-preserving
  `PyJson` type (section 4.2) and inserts keys in the Python order given in section 1.6.
  `error_type` values and nyxmon-authored `error_msg` templates are exact;
- `check_result.data` and `collector_incident.payload` text is produced by a
  Python-compatible `json.dumps` (default separators `", "`/`": "`,
  `ensure_ascii=True`, Python float repr; `sqlite_repo.py:_insert_on_connection`
  and the incident writes call bare `json.dumps`). Semantic equality is what
  the dashboard needs; byte equality makes the shadow diff trivial and costs
  one module (`pyjson`, section 4);
- notification decisions and every `check_notification_state` transition
  (`handlers.py:_should_notify_check_result`, `add_check_result`);
- the `collector_incident` payload schemas (stale lease, execution paused,
  delivery markers, the full site lifecycle payload of
  `site_connectivity.py:SiteConnectivityObserver.build_payload`);
- the text of every external message: the Telegram body
  (`notification.py:async_notify_check_failed`), the collector incident
  messages (`collector.py:_stale_batch_message`, `_paused_batch_message`),
  the site messages (`site_connectivity.py:_ongoing_message`,
  `_render_summary_line`, `_summary_line`), and the OpsGate ticket payload and
  prompt (`notification.py:_build_opsgate_ticket_payload`,
  `_build_opsgate_prompt`) up to the random boundary nonce and the expiry
  timestamp;
- environment variable names, defaults, bounds and fallback behaviour
  (section 1.4), CLI flags and defaults, exit codes, signal handling and lease
  release on shutdown;
- the operator-facing log lines listed in section 1.7 (message text; the
  logger name and level are kept, the timestamp format is matched).

**P2 — equivalent.** Same meaning, text may differ:

- `error_msg` derived from library exceptions (`str(exc)` of httpx, dnspython,
  ssl, smtplib, imaplib). For OS-level failures Rust renders the Python form
  `"[Errno N] <strerror>"` and `"timed out"` where the errno is known, because
  operators see these on the detail page; other library messages may differ;
- measured durations (`duration_ms`, `connect_time_ms`, `tls_handshake_ms`,
  `query_time_ms`, ping RTTs);
- log lines not listed in section 1.7; log formatting of the
  `LoggingNotifier` (used only without `--enable-telegram`);
- messages that embed a Python `set` repr, whose order depends on
  `PYTHONHASHSEED` and is therefore not stable even between two Python runs
  (`imap_config.py`, `json_metrics_config.py`, `dns_config.py`
  `validate`/`from_dict`). Rust renders the set elements sorted, in the Python
  set-literal format (`{'a', 'b'}`).

**P3 — intentional divergence.** Listed exhaustively in section 1.5. Anything
else that differs is a bug.

### 1.4 Environment variables (all P1)

| Variable | Read in | Default | Rule |
| --- | --- | --- | --- |
| `NYXMON_CHECK_BATCH_SIZE` | `repositories/interface.py:check_batch_size` | 5 | int, clamped to 1..100; non-int warns once, default |
| `NYXMON_PROCESSING_LEASE_SECONDS` | `collector.py:processing_lease_seconds` | 900 | int; <30 warns and clamps to 30; non-int warns, default |
| `NYXMON_SHUTDOWN_GRACE_SECONDS` | `collector.py:shutdown_grace_seconds` | 5 | Python `float()`, finite, 0..30, else warn and 5 |
| `NYXMON_NOTIFY_CONSECUTIVE_FAILURES` | `notification_policy.py:global_consecutive_failures` | 2 | int 1..100 else warn, default |
| `NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS` | `global_reminder_seconds` | 21600 | int 60..2592000 |
| `NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS` | `global_reminder_seconds` | 86400 | int 60..2592000 |
| `NYXMON_NOTIFY_REPEAT_FAILURES` | `_check_deprecated_env` | — | ignored; one warning per distinct value |
| `NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS` | `handlers.py:_notify_immediate_cooldown_seconds` | 3600 | positive int else warn, default |
| `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS` | `handlers.py:delivery_retry_seconds` | 0 | 0 or 60..3600 else warn, 0 |
| `NYXMON_SITE_CONNECTIVITY_MODE` | `site_connectivity.py:SiteConnectivityConfig.from_env` | off | `off`/`observe`/`enforce`, lowercased; unknown warns once, off |
| `NYXMON_SITE_PROBE_INTERVAL_SECONDS` | same | 60 | 15..600 |
| `NYXMON_SITE_PROBE_TIMEOUT_SECONDS` | same | 3 | 1..10 |
| `NYXMON_SITE_PROBE_IPV4_TARGETS` | `_parse_endpoints` | `1.1.1.1:443,8.8.8.8:443,9.9.9.9:443` | unset → default; explicitly empty → unobserved; invalid → warn, default |
| `NYXMON_SITE_PROBE_IPV6_TARGETS` | same | three `[v6]:443` | as above |
| `NYXMON_SITE_PROBE_DNS_NAMES` | `_parse_dns_names` | `cloudflare.com,google.com,quad9.net` | as above; whitespace or `/` invalidates |
| `NYXMON_SITE_DOWN_AFTER_FAILURES` | `from_env` | 2 | 1..10 |
| `NYXMON_SITE_RECOVERY_GRACE_SECONDS` | `from_env` | 900 | 60..3600 |
| `NYXMON_SITE_MAX_HOLD_SECONDS` | `from_env` | 10800 | 600..86400 |
| `NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS` | `from_env` | 900 | 60..86400 |
| `NYXMON_SITE_INCIDENT_REMINDER_SECONDS` | `from_env` | 21600 | 60..2592000 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `AsyncTelegramNotifier.__init__` | — | missing → warning, sends report "cannot tell" (`None`) |
| `OPSGATE_SUBMIT_BASE_URL`, `OPSGATE_SUBMIT_TOKEN` | same | "" | stripped, base `rstrip("/")`; both needed |
| `OPSGATE_APPROVAL_BASE_URL` | same | submit base | stripped, `rstrip("/")` |
| `OPSGATE_TICKET_EXPIRES_SECONDS` | `_parse_positive_int` | 14400 | positive int else warn |
| `OPSGATE_SUBMIT_TIMEOUT_SECONDS` | same | 10 | positive int else warn |
| `OPSGATE_SUBMIT_INCLUDE_WARNINGS` | `_parse_bool` | false | `1/true/yes/on` |

Parsing rules that must match Python exactly (pinned by fixtures,
section 7.2): values are `.strip()`ped first; integers follow Python `int()`
(optional sign, ASCII digits, single underscores between digits; Rust does not
accept non-ASCII Unicode digits, P3 D-6); floats follow Python `float()`
(including `inf`/`nan` spellings, which the callers then reject as
non-finite); "warn once" is per distinct `(variable, value)` for the lifetime
of the process (`lru_cache` / `_warned_*` sets).

Rust-only additions, all optional and absent in production unless stated:
`NYXMON_AGENT_LOCK_PATH` is **not** introduced (the lock path is derived,
section 3.6); no other new variable is introduced by the port.

### 1.5 Intentional divergences (P3, exhaustive)

- **D-1 (withdrawn in plan round 2.)** Non-standard JSON numbers (`NaN`,
  `Infinity`, `-Infinity`) and integers beyond 64 bits are *not* a
  divergence: `pyjson` parses and serialises them exactly like CPython
  (section 4.2), including the consequences (a NaN that ends up in
  `check_result.data` is serialised as `NaN`, SQLite's
  `CHECK (JSON_VALID("data"))` rejects the insert, and the result is lost
  exactly as in Python; a NaN in an unused field changes nothing; OpsGate
  bodies are encoded with `allow_nan=False` semantics, so a NaN makes the
  ticket submit fail as in httpx).
- **D-2 Set reprs** in configuration error messages are rendered sorted
  (section 1.3 P2; fixture normalisation in 7.2).
- **D-3 Shutdown of an in-flight batch.** Python abandons the batch thread
  (it may still persist a result after lease release, until the process
  exits). Rust aborts the batch task before releasing leases, so no result is
  written after the release. Release semantics are otherwise identical.
- **D-4 Executor panics** map to `error_type: "executor_exception"` with
  `error_msg: "panic"` (Python: the exception class name).
- **D-5 Schema.** Rust runs no DDL and refuses to start on a database whose
  schema is older than the contract (section 2). Python creates/upgrades
  tables itself.
- **D-6 Unicode digits.** Python's `int()`, `float()`, `str.isdigit()` and
  `re` `\d` accept non-ASCII digits in env values, JSON paths and numeric
  strings in check data (e.g. a ping `"timeout": "\u0662"`); Rust accepts
  ASCII digits only (a ping check then reports `configuration_error`
  "timeout must be a number", pinned by
  `config/ping/tests.rs::unicode_digits_are_not_numbers`).
- **D-7 (withdrawn in plan round 2,** see D-1: big integers are exact up to
  CPython's 4300-digit conversion limit, which `pyjson` enforces the same way.)
- **D-8 Lone UTF-16 surrogates** in JSON strings (`"\ud800"`): CPython keeps
  them in `str`; Rust strings cannot hold them. `pyjson` rejects such a
  document as a whole. Consequences, all accepted as P3 because no production
  endpoint emits lone surrogates: a json-metrics body becomes `json_error`
  where Python could have produced OK or a threshold result; a suppression
  payload suppresses nothing (fails open) where Python could have suppressed;
  a `health_check.data` value with a lone surrogate makes that check's
  executor return `configuration_error` ("check data is not valid JSON").
  The same runner result covers every `data` column the store cannot
  decode (integers over 4300 digits, text that is not UTF-8 or not JSON;
  `nyxmon_store::data_is_undecodable`, S2/S9a), where Python's
  `row_to_check` raises after the claim committed and strands the batch
  until the lease expires.
- **D-9 TLS library.** rustls instead of OpenSSL: no TLS 1.0/1.1, no SHA-1
  signatures, no RSA keys under 2048 bits. All production TLS endpoints are
  own, current services; the shadow run (section 9) verifies them.
  Server names: rustls represents only valid DNS names and IP addresses. A
  TLS server name it cannot represent (a scoped IPv6 literal such as
  `::1%lo0`, a trailing-dot IP such as `127.0.0.1.`, a name with `:`, a
  space or a leading hyphen) is sent without SNI where OpenSSL would send
  the raw name; with `verify=true` the TCP check fails closed as
  `tls_error` ("TLS handshake failed: server name cannot be verified: not a
  valid DNS name or IP address", retryable) before the handshake, even
  when OpenSSL's `X509_check_host` could match a certificate carrying that
  exact name. No hand-written OpenSSL host matching is attempted. No
  production check uses such a name (the TCP checks target ordinary DNS
  names on port 443). Pinned by `tcp/tests.rs`
  (`names_rustls_cannot_send_still_connect_without_verification`,
  `names_rustls_cannot_send_fail_verification`,
  `a_scoped_ipv6_host_follows_the_platform_resolver`).
- **D-10 Agent lock file** `<db>.agent.lock` (section 3.6); also added to the
  Python agent.
- **D-11 Non-object `health_check.data`.** Python maps NULL/empty to `{}` but
  the JSON text `null` (or a non-object) to a non-dict that later crashes
  inside handlers. Rust maps `null` to `{}` and keeps other non-objects as-is
  (`Check.data` is a raw `PyJson`, section 4.2) so each executor reproduces
  the Python configuration outcome pinned by the config fixtures; the
  notification-policy/suppression/site-dependency readers treat a non-object
  as "no configuration" instead of crashing the result handler.
- **D-12 Random values.** The SMTP correlation token and the OpsGate
  boundary nonce are random in both implementations; they are injected in
  fixtures and masked in parity comparisons (7.4).
- **D-13 Notifier proxies.** The Telegram and OpsGate requests ignore
  `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` (httpx honours them through
  `trust_env`). The notifiers use a small hyper HTTP/1.1 client so that
  httpx's per-phase timeouts can be reproduced (5.11); the production unit
  sets no proxy variable (checked on macmini 2026-10-14).
- **D-14 SMTP addresses.** The Rust SMTP executor sends only plain ASCII
  `from_addr`/`to_addr` values: `local@domain` (dot-atoms), optionally as
  `Display Name <local@domain>` (atoms and periods, or one quoted string
  without `"` and `\`) or `<local@domain>`; `to_addr` may list several
  separated by `,` or `, `; no `=?`, and `From: `/`To: ` plus the value fit
  one 78-character line. Any other value (non-ASCII text or whitespace,
  comments, groups, quoted local parts, domain literals, malformed lists,
  over-long values) is a `configuration_error` ("<field> is not supported
  by the Rust agent: ...") where Python would render and send it through
  its email policy. Production uses plain addresses only (2026-10-14:
  `monitor@xn--wersdrfer-47a.de`, `wersdoerfer.mailmon@gmail.com`,
  `nyxmon-local-probe@xn--wersdrfer-47a.de`). Pinned by
  `config/smtp/tests.rs::unsupported_address_forms_are_configuration_errors`
  and the `config_smtp.json` corpus.
- **D-15 Check and suppression proxies.** The HTTP family (http,
  json-http, json-metrics) and the maintenance-suppression fetch ignore
  `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`; httpx honours them
  through `trust_env` (the shared client of 5.5 has no proxy support). The
  production unit sets no proxy variable (checked in `/proc/<pid>/environ`
  on macmini, 2026-10-14). No code path of the Rust clients reads proxy
  variables.
- **D-16 DNS corners outside production** (owner parity policy
  2026-10-14; all four production DNS checks are A queries for ASCII
  names with an explicit `dns_server`):
  - a check URL with non-ASCII characters: dnspython IDNA-2008-encodes it
    ("practical" codec); Rust reports `configuration_error` "non-ASCII DNS
    names are not supported; use the IDNA (xn--) form"
    (`dns/wire/tests.rs::non_ascii_names_fail_closed`,
    `dns/tests.rs::non_ascii_names_are_a_configuration_error`);
  - malformed responses: Rust validates the message structure, names, the
    rdata of A, AAAA, MX, TXT, CNAME, NS, PTR, SOA, the OPT placement and
    the OPT option framing; dnspython also validates the rdata of every
    other known type, rdata of classes other than IN by their own formats,
    and the contents of known EDNS options. Rust accepts such a response
    where dnspython rejects it (UDP: ignored, TCP: nameserver dropped).
    No production nameserver sends malformed packets
    (`dns/wire/tests.rs::compression_pointers_must_point_backwards`,
    `::opt_options_must_fill_the_rdata`);
  - a `resolv.conf` `nameserver` that is a DNS-over-HTTPS URL: dnspython
    queries it over DoH; Rust reports `configuration_error`
    "DNS-over-HTTPS nameserver … is not supported"
    (`dns/tests.rs::doh_nameservers_in_resolv_conf_are_a_configuration_error`);
    `domain`/`search` lines are not parsed (dnspython raises on invalid
    names there; the check never uses a search list);
  - malformed TCP responses: every response Rust cannot parse drops the
    nameserver (`unexpected_error` once all are dropped); dnspython drops
    it on `FormError` but retries other parse exceptions (e.g. an A rdata
    of the wrong length) until the lifetime ends (`timeout`).
- **D-17 Non-UTF-8 `PATH` entries.** The ping binary lookup
  (`shutil.which`) skips `PATH` directories whose name is not UTF-8, which
  Python would search through `surrogateescape`; the `/sbin`, `/bin`,
  `/usr/bin`, `/usr/sbin` fallbacks still apply. Pinned by
  `ping/tests.rs::which_skips_entries_that_are_not_utf8`.

- **D-18 IMAP exotic inputs.** A truthy `host` that is a JSON list or
  object is a `configuration_error` ("host is not supported by the Rust
  agent: not a string"); Python accepts it and fails later in ways that
  depend on its contents. An INTERNALDATE whose UTC year leaves 1..=9999 is
  `unexpected_error` "year <y> is out of range" as on glibc (Python on
  macOS fails `mktime` before 1900 instead). When `Internaldate2tuple`
  raises (a month name that is not English, year 0) the
  `parsedate_to_datetime` fallback is assumed to fail ('Invalid date value
  or format "<line>"', or the `UnicodeDecodeError` text), as it does for
  FETCH response lines (`N (UID ... INTERNALDATE "...")`); a line it could
  parse would give Python a date instead. A literal size beyond `usize` is
  not treated as a literal. No production server sends such values: the 2
  production IMAP checks (2026-10-14) use a string host, implicit TLS, a
  plain `password`, ASCII subjects and Dovecot, whose dialogue the
  `imap_dialogues.json` fixture models. Pinned by `config/imap/tests.rs`
  (`configuration_matches_python`, `out_of_range_dates_are_errors`).

### 1.6 Result `data` shapes per check type (P1)

Order of keys as written. `[x]` = present only under the stated condition.
`A` = `"attempt", "attempts"` in this order.

Runner-level (`adapters/runner/async_runner.py:_run_one`):

- unknown type: `error_type="unknown_check_type"`,
  `error_msg="No executor registered for check type '<t>'. Registered types: http, json-http, dns, tcp, json-metrics, imap, smtp, ping"`
  (registry insertion order, `_preregister_executors`), `check_type`.
- executor crash: `error_type="executor_exception"`, `error_msg`, `check_type`.

**http / json-http** (`executors/http_executor.py:HttpCheckExecutor.execute`):

- config error: `error_type="configuration_error"`, `error_msg`, `attempt=0`,
  `attempts=0`.
- OK: `{}`; `[attempt, attempts, duration_ms]` if `attempt>1 or attempts>1`;
  `[status_code]` if `expected_status` set; `[location]` if
  `expected_location` set.
- `unexpected_status`: `error_type`, `error_msg="Expected HTTP <e>, got <s>"`,
  `expected_status`, `status_code`, A.
- `http_error` (no `expected_status`, status not 2xx): `error_type`,
  `error_msg="HTTP <s>"`, `status_code`, A.
- `unexpected_location`: `error_type`,
  `error_msg="HTTP Location header does not match the configured target"`,
  `expected_location`, `location` (null when absent), A.
- `timeout` / `connection_error` / `request_error`: `error_type`, `error_msg`
  (P2), A. Retried with `retry_delay` while attempts remain.
- `unexpected_error`: `error_type`, `error_msg`, A.
- Retry on status: only when `status_code in retry_status_codes` and
  `attempt < attempts`, both with and without `expected_status`.

**json-metrics** (`executors/json_metrics_executor.py`):

- config error / http_error / body_too_large / timeout / request_error /
  json_error / unexpected_error: exactly `error_type`, `error_msg`
  (no attempt keys). `http_error` message `"HTTP <s>"` for `>= 400`, retried
  only for 5xx while `attempt < retries`. `body_too_large` messages:
  `"response body of <declared> bytes exceeds the <limit> byte limit"` (declared
  `Content-Length` all ASCII digits and over the limit) and
  `"response body exceeds the <limit> byte limit"` (decoded stream over the
  limit; the limit applies to decompressed bytes).
- threshold failure: `error_type="threshold_failed"`,
  `error_msg="<n> critical: p1, p2; <m> warning: p3"` (each part only if
  non-empty), `failures` (list), `duration_ms`; status `error` if any failure
  is critical, else `warning`.
- each failure: `path, op, expected, actual, severity`; for a missing path
  `path, op, expected, actual=null, reason="path_missing", severity`; when the
  Python `json.dumps(actual, default=str, ensure_ascii=False)` is longer than
  200 code points, `actual` becomes the first 200 code points + `"…"` (in
  place) and `actual_truncated=true` is appended.
- OK: `duration_ms`.

**tcp** (`executors/tcp_executor.py`):

- config error: `error_type`, `error_msg`; missing host: `error_msg="host is required (set check.url or data.host)"`.
- OK: `host, port, tls_mode, [starttls_protocol if tls_mode=starttls], connect_time_ms, [tls_handshake_ms], [cert_days_remaining], attempt, attempts`.
- structured error: `error_type, error_msg, host, port, tls_mode, attempt, attempts, [starttls_protocol]`, then the error's own data in order
  (`connect_time_ms, tls_handshake_ms` for `cert_missing`;
  `severity="warning", cert_days_remaining, connect_time_ms, tls_handshake_ms`
  for `cert_expiry` — the result status stays `error`;
  `starttls_response, starttls_stage` for `starttls_rejected`).
- error types: `connect_timeout` ("Connection to h:p timed out", retryable),
  `connection_error` ("Connection to h:p failed: <os error>", retryable),
  `tls_timeout`, `tls_error` ("TLS handshake failed: …", retryable),
  `starttls_timeout`, `starttls_connection_closed` (retryable),
  `starttls_rejected`, `starttls_protocol_error`, `cert_missing`,
  `cert_expiry` (not retryable), `unexpected_error` (`host, port, tls_mode, A`;
  also when the TLS close fails, 5.12).

**dns** (`executors/dns_executor.py`):

- OK: `resolved_ips, query_time_ms, response_code, questions, rrset, dns_server, [source_address]`.
- mismatch: `error_type="resolution_mismatch", expected, actual, query_time_ms, response_code, questions, rrset, dns_server, [source_address]`.
- errors: `error_type, error_msg` with `nxdomain` ("Domain <url> does not
  exist"), `timeout` ("DNS query timed out for <url>"), `no_answer` ("No
  answer received for <url>"), `configuration_error`, `unexpected_error`
  (everything else, e.g. SERVFAIL/NoNameservers; P2 message).
- metadata: `response_code` as dnspython text (`"NOERROR"`), `questions`
  `["<qname with trailing dot> IN <TYPE>"]`, `rrset` = record texts,
  `dns_server` = configured server or `"system"`.

**ping** (`executors/ping_executor.py`): base
`host, timeout (float), count, interval (float)`; then `[target]`,
`[hostname if target != host]`; then on attempts
`packets_sent, packets_received, packet_loss_percent, attempts (list)` and on
success `rtt_min_ms, rtt_max_ms, rtt_avg_ms (round 3), rtt_list_ms`. Errors
prefix `error_type, error_msg` before the base keys. Error types
`configuration_error`, `dns_timeout`, `dns_error`, `ping_unavailable`,
`permission_error`, `execution_error`, `unreachable`, `timeout`. Attempt
records: `{attempt, status="ok", rtt_ms}`, `{attempt, status="timeout",
error_msg}`, `{attempt, status="error", error_msg}`, `{attempt,
status="unreachable"|"timeout", error_msg, returncode}`. Messages use Python
`format(x, "g")` for timeouts.

**smtp** (`executors/smtp_executor.py`):

- OK: `response_code=250, response_message="accepted", attempts, subject, token, from, to`.
- error: `error_type, error_msg, attempts, [smtp_code]`; error types
  `configuration_error` (only `error_type, error_msg`), `auth_error`,
  `temporary_failure` (4xx, retried with `retry_delay`), `smtp_error`,
  `timeout` ("connection timed out"), `connection_error`, `tls_error`,
  `recipient_refused`, `unexpected_error`.

**imap** (`executors/imap_executor.py`):

- OK: `matched_uids` (list of UID strings, ascending INTERNALDATE),
  `latest_internaldate` (Python `datetime.isoformat()` in UTC, `+00:00`).
- error: `error_type, error_msg, attempts, [severity]`; `no_recent_message`
  ("No messages with subject '<s>' within <m> minutes", status per
  `no_recent_message_severity`, `severity` key present), `transient_failure` (unreachable with imaplib,
  see 5.14),
  `execution_error` ("<method> failed: <typ> <Python repr of data list>"),
  `tls_error`, `unexpected_error`; `configuration_error` has only
  `error_type, error_msg`.

**Annotations added by the result handler** (`handlers.py:add_check_result`):
`notification_suppressed` (appended, from
`notification_suppression.py:notification_suppression_details`) and
`site_connectivity` (`{"held": bool, **reason}`, appended or replaced in
place, `handlers.py:_annotate_site_connectivity`).

**Collector-internal result** (`collector.py:_record_stale_check`):
`error_type="stale_processing_lease"`, `error_msg="Check processing lease expired; the previous worker did not persist a result"`, `collector_internal=true`.

### 1.7 Operator-relevant log lines (P1 text)

Logged with the Python logger name as target and the format
`%(asctime)s - %(name)s - %(levelname)s - %(message)s` (asctime
`YYYY-MM-DD HH:MM:SS,mmm`, local time) on stderr. These lines are referenced
by `docs/configuration.md`, `docs/usage.md`, runbooks or tests and keep their
exact text:

- `Database file not found: <path>`; `Initializing monitoring services with database: <path>`;
  `Telegram notifications enabled`; `Monitoring started with <n>s check interval`;
  `Received signal <n>, shutting down...`
- `result handling failed for check_id=<id>`
- `dropped late result for deleted check_id=<id>`;
  `stored late result without changing newer claim for check_id=<id>`
- `notification state changed concurrently for check_id=<id>; retrying`;
  `notification state remained contended for check_id=<id>; persisting result without changing alert state`
- `reclaimed <n> expired check processing lease(s); tracked as a single collector incident (<k> alert(s) so far)`
- `reclaimed <n> expired leases without draining the backlog; the remainder is handled by the next collector iteration`
- `check batch exceeded its <s>s execution/result deadline; new executions are paused while lease recovery and alerts continue`
- `abandoned check batch is still running; new executions are paused`
- `failed to release check leases on shutdown`
- `configured processing lease <c>s is below the estimated maximum check runtime <r>s; using <e>s`
- `Cleaned up <n> old check results in <b> batch(es)`; `Result cleanup stopped after …`
- `site connectivity observation is <mode>`;
  `site connectivity incident <id> opened: <paths> down`;
  `site connectivity incident <id> closed after <dur> (summary: yes|no)`;
  `site connectivity recheck for release <iso> completed`
- all warn-once configuration warnings of section 1.4 and
  `notification_policy.<k> for check_id=<id> is invalid (…)`,
  `site_dependency for check_id=<id> is invalid (…)`,
  `check_id=<id> requires the unobserved path(s) …`
- `Telegram notification delivered`;
  `Failed to send Telegram notification: …` (token redacted, body ≤ 500 chars);
  `Telegram rejected the MarkdownV2 body; resending as plain text`;
  `Cannot send Telegram notification: missing credentials`;
  `OpsGate …` submit lines of `_create_opsgate_ticket`.

Security rule (P1): no log line, error string or `error_msg` may contain the
Telegram bot token. `reqwest::Error` includes the URL in `Display`; the
notifier must use `err.without_url()` and additionally replace the token with
`<redacted>` (`notification.py:_post_message`, `cli.py:setup_logging`).

---

## 2. Schema contract

### 2.1 Ownership

Django migrations own the schema. The live schema (verified on macmini) is
exactly what `manage.py migrate` produces up to
`nyxboard.0013_site_connectivity_state`: `health_check` and `check_result`
from Django (`AUTOINCREMENT`, `CHECK ("next_check_time" >= 0)`,
`CHECK (JSON_VALID("data") OR "data" IS NULL)`, deferred FKs),
`check_notification_state` and `collector_incident` from the
`SeparateDatabaseAndState` migrations 0011–0013.

### 2.2 The Rust agent runs no DDL

The Rust agent never executes `CREATE`, `ALTER`, `DROP` or `PRAGMA
journal_mode=…`. At startup, before taking any claim, `nyxmon-store` verifies:

1. Tables and columns exist (via `PRAGMA table_info`):
   - `health_check`: `id, service_id, name, check_type, url, check_interval, next_check_time, processing_started_at, status, disabled, data`
   - `check_result`: `id, health_check_id, status, data, created_at`
   - `check_notification_state`: `check_id, failure_count, last_attempt_count, last_immediate_at, last_notified_at, first_failure_at, held_since, attempt_seq, attempt_at`
   - `collector_incident`: `incident_key, opened_at, last_alert_at, alert_count, payload`
2. `django_migrations` exists and contains
   `('nyxboard', '0013_site_connectivity_state')` (the minimum migration
   level, constant `MIN_NYXBOARD_MIGRATION` in `nyxmon-store`).
3. `django_session` is optional (the cleaner probes for it every cycle, like
   `delete_old_results_async`).

On failure the agent logs one error naming every missing table/column/
migration and the remedy (`manage.py migrate`) and exits with status 1
before claiming anything. `nyxmon-agent --check-schema --db PATH` runs only
this verification (read-only open) and exits 0/1; the deploy role runs it
before switching the unit (section 8.4). The journal mode is read and logged
at INFO.

### 2.3 The Python `_ensure_schema` code

`sqlite_repo.py:_ensure_schema` and `_upgrade_notification_state_schema` stay
unchanged in the Python agent: they are idempotent no-ops on a migrated
database, they keep agent-only test databases working, and the Python agent
is the rollback target. Rule from now on (documented in
`docs/agent-rs.md` and enforced by the contract test below): **no new DDL is
added to the Python agent**. Every future change to an agent table is a
Django migration plus an update of the Rust contract. The Python DDL is
removed when the Python agent is retired (open question Q6).

### 2.4 Keeping schema changes in sync

- `agent-rs/fixtures/schema/schema.sql` is the `sqlite3 .schema` dump of a
  database freshly migrated by `manage.py migrate` plus the `INSERT`s of its
  `django_migrations` rows. Rust store tests build every test database from
  this file, so they always run against the real Django schema.
- `agent-rs/fixtures/schema/agent_tables.json` lists the columns of the four
  agent tables and `MIN_NYXBOARD_MIGRATION`.
- `tests/unit/test_agent_rs_schema_contract.py` (Python, runs in `just test`
  and CI) migrates a temporary database with the test settings, regenerates
  both files in memory and fails when they differ from the committed ones,
  with the message "run `just rs-fixtures` and update the Rust schema
  contract". A migration that touches an agent table therefore cannot merge
  without a conscious update of `nyxmon-store` and this plan's section 2.2.
- A new required column raises `MIN_NYXBOARD_MIGRATION` and is a breaking
  agent release: the role's `--check-schema` pre-flight refuses to switch
  before migrations ran (the role already runs `migrate` before restarting
  services, `nyxmon_deploy/tasks/django.yml`).

---

## 3. Concurrency and database design

### 3.1 Runtime shape

One tokio runtime (`multi_thread`, `worker_threads = 2`,
`max_blocking_threads = 32` for `getaddrinfo`), plus one dedicated OS thread
owning the SQLite connection. Long-lived tasks:

- collector loop (section 5.8), which spawns one batch task per iteration;
- site observer loop (only when mode ≠ off);
- cleaner loop (unless `--disable-cleaner`);
- main task: signal handling and orchestrated shutdown.

The Python agent's threads/portals/`to_thread` plumbing
(`docs/concurrency-overview.md`) is not reproduced; only its observable
ordering is (sections 3.4, 5.8).

### 3.2 rusqlite on a dedicated thread (decision)

Decision: `rusqlite` with the `bundled` feature (SQLite ≥ 3.46, `RETURNING`
needs ≥ 3.35) on one dedicated thread ("DB actor"). Callers send a boxed
closure `FnOnce(&mut Connection) -> T` over an mpsc channel and await a
oneshot reply. Every public store operation is one closure = one transaction,
mirroring the Python per-operation connection
(`sqlite_repo.py`, every method opens its own `aiosqlite.connect`).

Why not sqlx: its SQLite driver also runs a background thread per connection,
adds a pool and async transaction lifetimes we do not need, and compile-time
checked queries need a database at build time. The Python code is a set of
short, explicitly bounded transactions with `BEGIN IMMEDIATE`; a synchronous
API expresses those 1:1 and keeps transaction boundaries reviewable. One
connection serialises the agent's own writes, which is harmless: every
operation is milliseconds, results are already persisted serially by Python
(section 3.4), and the only waits are on Django/backup locks, bounded by
`busy_timeout`.

Connection settings: `busy_timeout = 5000 ms` (Python's `sqlite3.connect`
default `timeout=5.0`), `PRAGMA foreign_keys = ON` (Python sets it in
`_persist_check_result_async`; enabling it for every agent transaction is
safe because every insert targets an existing check inside the same
transaction and no agent delete has FK children), opened with
`SQLITE_OPEN_READ_WRITE` only (no `CREATE`: the file must exist, like the
`db_path.exists()` check in `cli.py:start_agent`). A panicking closure or a
dead actor thread is fatal: log and exit 1 (systemd restarts the unit).

### 3.3 Journal mode and locking

The production database is in `journal_mode=delete`, set by nobody (SQLite
default). The Rust agent does not change it: the journal mode is a persistent
database property shared with Django and the backup/restore roles
(`nyxmon_backup` copies the file with services stopped). Switching to WAL is a
separate decision (Q5). Consequences that the design respects:

- Every agent write transaction starts with `BEGIN IMMEDIATE`, including the
  ones Python ran as implicit deferred transactions (`list_due_checks_async`,
  `reclaim_stale_checks_async`, `release_claims_async`, the result/notification
  writes, `reschedule_checks_async`, the cleaner deletes). A deferred
  read-then-write transaction in rollback-journal mode can fail with
  `SQLITE_BUSY` on lock upgrade without the busy handler helping; taking the
  RESERVED lock up front removes that failure mode and changes no outcome.
- Read-only operations (`get_notification_state`, `list_held_checks`, counts,
  `get_collector_incident`, `list_checks`) run in autocommit.

### 3.4 Claim, complete, lease semantics (P1, SQL verbatim)

All statements are copied from `sqlite_repo.py`; parameter order and guards
are part of the contract.

- **Claim** (`list_due_checks_async`): one `UPDATE health_check SET
  status='processing', processing_started_at=:now WHERE id IN (SELECT id …
  WHERE next_check_time <= :now AND status='idle' AND disabled=0 ORDER BY
  next_check_time ASC, id ASC LIMIT :batch) RETURNING <all columns>`.
  `:batch = check_batch_size()`. Each returned row becomes a `Check` whose
  `claim_started_at = processing_started_at` (`domain/models.py:Check.__init__`).
- **Schedule on result** (`Check.schedule_next_check`): when the result
  arrives, `next_check_time = int(now) + check_interval`, `status='idle'`,
  `processing_started_at=0`; `claim_started_at` is kept.
- **Complete** (`_complete_on_connection`): `UPDATE health_check SET
  status=?, next_check_time=?, processing_started_at=? WHERE id=? AND
  ((status='processing' AND processing_started_at=:claim) OR (status<>'processing' AND :claim=0))`.
- **Persist** (`_persist_check_result_async`), inside one `BEGIN IMMEDIATE`:
  1. if `complete_check`: run Complete; `completion_applied` = rowcount > 0;
  2. if not completing or completion not applied: if the check row is gone →
     rollback, log `dropped late result for deleted check_id=…`, return
     `false`; else (completing) log `stored late result without changing newer claim …`;
  3. insert the result (`INSERT INTO check_result (health_check_id, status,
     data, created_at) VALUES (?, ?, ?, datetime('now'))`);
  4. if completing and completion not applied: commit, return `false` (the
     notification state is not touched and nothing is sent);
  5. if a transition `(expected, next)` is given: read the current state row
     (`NotificationState.from_row`, missing row = all zeros); if it differs
     from `expected` → rollback, raise `NotificationStateConflict`; if
     `next != expected` → upsert all eight columns;
  6. else if `hold_marker` given → `_mark_held_since` (update where
     `held_since = 0`; if no row updated and no row exists, insert a row with
     only `held_since`);
  7. commit, return `true`.
- **Stale reclaim** (`reclaim_stale_checks_async`), one transaction: probe
  `status='processing' AND (COALESCE(processing_started_at,0)=0 OR
  processing_started_at <= now-lease)`; if none, return empty without
  writing; stamp `processing_started_at=now` on processing rows without a
  timestamp (legacy claims get a full lease); then `UPDATE … SET
  status='idle', processing_started_at=0 WHERE id IN (… processing_started_at > 0
  AND <= :stale_before ORDER BY processing_started_at ASC, id ASC LIMIT :batch)
  RETURNING …`. Returned rows have `processing_started_at = 0`, hence
  `claim_started_at = 0`, so their later stale result completes through the
  `status<>'processing' AND 0=0` branch unless a newer claim exists.
- **Release on shutdown** (`release_claims_async`), one transaction: for each
  owned claim `UPDATE … SET status='idle', processing_started_at=0,
  next_check_time = :now + MAX(1, check_interval) WHERE id=? AND
  status='processing' AND processing_started_at=:claim AND :claim > 0`. Only
  this process's claims, never another collector's, never a completed check,
  no result, no notification-state change.
- **Held set / reschedule / counts** (`list_held_checks_async`,
  `reschedule_checks_async` with `status='idle' AND disabled=0 AND
  next_check_time > :run_at`, `count_held_checks_async`,
  `count_processing_claims_before_async` with `processing_started_at > 0 AND
  < :epoch`): verbatim.
- **Notification ack** (`_acknowledge_notification_attempt_async`):
  `UPDATE check_notification_state SET attempt_at=0 WHERE check_id=? AND
  attempt_seq=? AND attempt_at<>0`.
- **Collector incidents**: `get`, `claim` (`_claim_collector_incident_alert_async`:
  new row → `opened_at=last_alert_at=now, alert_count=1`, payload with
  `delivery_pending=true, delivery_attempt=1`; existing →
  `should_notify = now - last_alert_at >= max(1, reminder)`, payload = new
  payload or existing payload, with fresh delivery markers when granted,
  otherwise the existing markers carried forward), `open` (create silently
  with `last_alert_at=0, alert_count=0`, or replace the payload carrying the
  markers forward), `set_payload` (replace payload verbatim, `None` when no
  row), `acknowledge` (compare `delivery_attempt` and strip both markers in
  one `BEGIN IMMEDIATE`), `close` (delete, returning the old row). Payloads
  that fail to decode or are not objects read as `{}`
  (`_row_to_collector_incident`).
- **Cleaner batch** (`delete_old_results_async`): if `django_session` exists,
  delete up to `batch_size` expired sessions (`expire_date < datetime('now')`,
  oldest first) and commit; then select up to `batch_size` ids with
  `created_at < datetime('now', '-<retention> seconds') ORDER BY id`, delete
  them, commit, return the count.

### 3.5 Reading rows

`row_to_check`: `disabled` as bool, `data` decoded from TEXT (NULL or empty
→ `{}`; D-11). `HeldCheck` per `list_held_checks_async`. Integer columns read
as `i64`; NULL integers read as 0 where Python uses `int(x or 0)`.

### 3.6 Coexistence rules

The Python and the Rust agent must never both run against the production
database. Enforcement, in layers:

1. **One unit.** Both implementations run as the same systemd unit
   `nyxmon-monitor`; the implementation is chosen by a drop-in
   (section 8.4). Backup/restore roles stop that unit by name
   (`nyxmon_backup_service_stop_order`), so they keep working unchanged.
2. **Agent lock.** Both agents take a non-blocking exclusive `flock` on
   `<db path>.agent.lock` (e.g. `/home/nyxmon/site/db.sqlite3.agent.lock`)
   before touching the database and hold it until exit. A second agent on the
   same database logs `another nyxmon agent holds <lock path>; refusing to start`
   and exits 1. The lock is a separate file (never `flock` on the database
   file: closing any extra descriptor of the database would drop SQLite's own
   POSIX locks). The name matches the existing `db.sqlite3*` rsync excludes
   and the `sqlite_privacy.yml` mode 0600 normalisation, so it survives deploys.
   The Python side is a ten-line change to `cli.py:start_agent` (slice S2).
   A rollback to a Python commit older than that change is still safe because
   of layer 1.
3. **Shadow mode.** The only sanctioned concurrent run is a shadow agent on
   a *copy* of the database (`--shadow`, section 9.1), which has its own lock
   file. `--shadow` forces the logging notifier, ignores `TELEGRAM_*` and
   `OPSGATE_*`, and refuses to start if `--enable-telegram` is also given.
4. **Read-only tools.** `nyxmon-agent --check-schema` and `nyxmon-agent
   run-check` open the database with `mode=ro` and take no lock.

Even without the lock, claims are race-safe (atomic claim, claim-time guard),
but two site observers would overwrite each other's `site:connectivity`
payload without compare-and-swap and both would alert; that is why the lock
is mandatory, not advisory.

### 3.7 Persisting results serially

Python consumes batch results one at a time (`async_runner.py:_async_run_batch`
awaits `receive(result)`, which blocks on `result_received` in a worker
thread): suppression fetch, decision, persist, Telegram, OpsGate and
acknowledgement of one result finish before the next result is handled,
while executions continue concurrently. Rust keeps this: executors run
concurrently in a `JoinSet`, completed results go through one consumer loop
in completion order. This ordering is what makes the
`NotificationStateConflict` retry practically unreachable and is part of the
batch deadline budget (`RESULT_HANDLING_BUDGET_SECONDS = 60` per check).

---

## 4. Crate layout, key types, crate choices

### 4.1 Workspace

```
agent-rs/
  Cargo.toml              # workspace, [profile.release] lto="thin", codegen-units=1, strip=true, panic="unwind"
  Cargo.lock              # committed; all builds --locked
  rust-toolchain.toml     # pinned stable toolchain (Q9)
  PLAN.md
  crates/
    nyxmon-core/          # pure logic, no I/O, no tokio
    nyxmon-store/         # rusqlite actor, schema verification, all SQL
    nyxmon-checks/        # executors, TLS/happy-eyeballs helpers
    nyxmon-notify/        # Telegram, OpsGate, logging notifier
    nyxmon-agent/         # binary: CLI, runtime, collector, cleaner, site observer I/O
  fixtures/               # shared JSON fixtures + generator (section 7.2)
    generate.py
    schema/
    *.json
  parity/                 # offline parity harness and shadow comparison (section 7.4, 9)
```

Splitting into crates enforces the purity boundary (everything
fixture-tested lives in `nyxmon-core`) and lets slices S2–S8 proceed in
parallel with few merge conflicts.

### 4.2 Key types (nyxmon-core unless noted)

- `PyJson`: the value type for every JSON document the agent reads or
  writes (check data, result data, incident payloads, metrics and
  suppression bodies): `Null | Bool | Int(i64) | BigInt(num_bigint::BigInt) |
  Float(f64, NaN/±inf allowed) | Str(String) | List(Vec<PyJson>) |
  Dict(IndexMap<String, PyJson>)` (insertion order, duplicate keys: last
  value wins at the first key's position, as CPython). `serde_json` is not
  used for data values; it may be used for HTTP bodies we build ourselves.
- `pyjson` (S1): a parser equivalent to `json.loads` (CPython scanner:
  `NaN`, `Infinity`, `-Infinity`, big integers, floats overflowing to
  `inf`, whitespace rules, escapes; lone surrogates rejected, D-8; integer
  tokens longer than CPython's default `sys.int_info.default_max_str_digits`
  = 4300 digits (sign excluded) are rejected exactly like CPython's
  `ValueError: Exceeds the limit (4300 digits) for integer string conversion`,
  so such a metrics body is `json_error` and such a suppression payload fails
  open — boundary fixtures at 4300/4301 digits) including
  `json.loads(bytes)` encoding detection (`json.detect_encoding`: UTF-8 with
  or without BOM, UTF-16/32 LE/BE by BOM or null-byte pattern);
  `dumps(&PyJson, DumpOpts { ensure_ascii, sort_keys, indent, separators,
  allow_nan })` matching CPython `json.dumps` (escapes with lowercase
  `\uXXXX`, surrogate pairs for astral characters, `NaN`/`Infinity`
  literals, float repr), `py_float_repr(f64)` (CPython
  `repr`), `py_format_g(f64)` (`format(x, "g")`), `py_round(f64, n)`
  (CPython `round`, via exact decimal formatting), `py_truthy(&PyJson)`,
  `py_cmp(op, &PyJson, &PyJson) -> Result<bool, TypeError>` implementing CPython
  rich comparison over JSON values (bool is an int; int/bigint/float compare
  exactly, NaN compares false except `!=`;
  `str` code-point order; list lexicographic with recursive element
  comparison; dict only `==`/`!=`, deep with numeric equality; mixed types:
  `==` false, `!=` true, ordering `TypeError`), `json_kind(&PyJson)`
  (`notification_suppression.py:_json_kind`), `py_int(&str)`,
  `py_float(&str)`, `py_bytes_repr(&[u8])`, `char_len(&str)` (Python `len`),
  `py_int_of(&PyJson)` (Python `int()` on a JSON value: bool → 0/1, float
  truncates, numeric strings parse, inf/nan → error).
- `env`: `EnvSource` (real or map, for tests), `WarnOnce`, and one function
  per variable in section 1.4.
- `Check { check_id, service_id, name, check_type, url, check_interval,
  next_check_time, processing_started_at, status, disabled, data: PyJson
  (raw, any JSON type, D-11), claim_started_at }` with
  `schedule_next_check(now)`.
- `CheckResult { check_id, status: ResultStatus, data: IndexMap<String, PyJson> }`,
  `ResultStatus { Ok, Warning, Error }` (`"ok"/"warning"/"error"`).
- `NotificationState` (eight `i64` fields, `cleared()`, `with_streak_reset()`,
  field-wise equality), `NotificationDecision { should_notify, expected,
  next_state, held, attempt_seq, now }`.
- `CollectorIncident`, `CollectorIncidentAlert`, `HeldCheck`.
- Config structs per check type with `from_data(&PyJson) -> ConfigOutcome`
  where `ConfigOutcome = Ok(cfg) | ConfigError(msg) | PyException(name)`
  (`PyException` reproduces `TypeError`/`AttributeError` paths that Python
  turns into `executor_exception`).
- `policy::NotificationPolicy`, `site::{SiteMode, PathState, PathSnapshot,
  SiteSnapshot, SiteDependency, SiteConfig, Observer}` (section 6).
- `nyxmon-store`: `Store` (cloneable handle to the actor) with one async
  method per operation of section 3.4; `NotificationStateConflict` error.
- `nyxmon-checks`: `trait Executors { fn registered_types(&self) -> &[&str]; fn execute(&self, check: Check, batch: Arc<BatchCtx>) -> Option<ExecFuture> }`
  (`registry.rs`; `ExecFuture` is a boxed `Result<CheckResult, PyException>`
  future, `None` = no executor for the type), `BatchCtx` (the batch's HTTP
  session and retry sleeper), `Registry` with the Python registration order
  (S9a).
- `nyxmon-notify`: `trait Notifier { fn notify_check_failed(&self, check: &Check, result: &CheckResult) -> NotifyFuture }`
  (a boxed future of `Result<Delivery, NotifyRaised>`, so the trait is
  object-safe) with `Delivery { Delivered, Failed, Unknown }` mirroring
  `True/False/None` (`notification.py:Notifier.notify_check_failed`; only
  `Failed` counts as a failure everywhere). `NotifyRaised` is the
  `OverflowError` Python raises out of `_build_opsgate_ticket_payload` when
  `OPSGATE_TICKET_EXPIRES_SECONDS` pushes the expiry beyond `datetime`'s
  range; the result handler treats it like any exception from
  `add_check_result`.

### 4.3 Crate choices

| Need | Crate | Notes |
| --- | --- | --- |
| async runtime | `tokio` (rt-multi-thread, net, time, process, signal, sync, fs, macros) | |
| HTTP checks, json-metrics, suppression | own HTTP/1.1 client on `hyper` 1 (`client::conn::http1`) + `tokio-rustls` (ring) + `webpki-roots` + `cookie_store` (`preserve_order`) + `flate2` for our own streaming decoder (S5 review: reqwest cannot express httpx's per-read timeouts or its request target) | httpx 0.28 without brotli/zstandard installed (`uv.lock`) sends `Accept-Encoding: gzip, deflate` and speaks HTTP/1.1 only; URLs, redirects and decoding are reimplemented to match httpx (5.5); certifi ↔ webpki-roots (both the Mozilla bundle) |
| Telegram, OpsGate | `hyper` 1 + `tokio-rustls` + `webpki-roots` (S8) | bodies we build ourselves |
| TLS for tcp/smtp/imap | `tokio-rustls` + `rustls-native-certs` | Python `ssl.create_default_context()` uses the system store |
| certificate expiry | `x509-parser` (notAfter only) | `tcp_executor.py:_cert_days_remaining` |
| DNS checks | own stub resolver on tokio UDP/TCP sockets (`nyxmon-checks/src/dns.rs`, `dns/wire.rs`; S5b) | replaces dnspython: its resolution loop (lifetime, backoff, nameserver dropping, TCP fallback), response matching and record texts are reproduced directly, which a resolver library would hide (section 5.6) |
| system resolution (probes, ping, happy eyeballs, HTTP clients) | libc `getaddrinfo` via `tokio::net::lookup_host` | same resolver semantics as Python (see 8.1) |
| IMAP | hand-rolled `imaplib.IMAP4` emulation (≈ 900 lines, S7b) | `async-imap` cannot reproduce the error texts the dashboard shows (`command: UID => socket error: EOF`, `UID command error: BAD [b'...']`, `select failed: NO [...]`), imaplib's response store or its quoting; see 5.14 |
| SMTP | hand-rolled (≈ 400 lines) | `lettre` lacks smtplib's error taxonomy, CRAM-MD5 and the recipient-refused semantics; see 5.7 |
| SQLite | `rusqlite` (`bundled`) | section 3.2 |
| CLI | `clap` (derive) | |
| logging | `tracing` + `tracing-subscriber` (fmt, custom formatter) | section 1.7 format |
| JSON | own `pyjson` parser/serialiser on `indexmap` + `num-bigint` (4.2); `serde`/`serde_json` only for request bodies we build | CPython `json` parity incl. NaN and big ints |
| time | `chrono` (UTC formatting, ISO, `datetime.isoformat` parity) | |
| regex | `regex` (ping/IMAP/STARTTLS patterns; the one lookbehind in `ping_executor.py:_SOURCE_RE` is implemented as a manual preceding-char check) | |
| unicode categories | `unicode-general-category` | `_sanitize_untrusted_text` (Cc, Cf, Zl, Zp) |
| locking | `rustix` `flock` (or `fs4`) | section 3.6 |
| randomness | `rand` (`token_hex`) | SMTP token, OpsGate boundary |
| tests | `tokio-test`, `rcgen` (test certificates), `tempfile` | |

### 4.4 Lean binary goals

- Stripped release binary ≤ 20 MiB.
- Steady-state RSS ≤ 20 MiB, peak during a 5-check batch ≤ 40 MiB
  (Python today: 47 MiB). If glibc malloc arenas exceed the budget,
  `MALLOC_ARENA_MAX=2` is set in the drop-in before considering another
  allocator.
- Startup to first claim ≤ 1 s on macmini.
- Measured during the shadow run and recorded in the cutover report.

---

## 5. Per-module port mapping

| Python | Rust |
| --- | --- |
| `entrypoints/cli.py` (`setup_logging`, `run_monitoring_services`, `start_agent`) | `nyxmon-agent/src/main.rs`, `cli.rs`, `logging.rs`, `shutdown.rs` |
| `bootstrap.py`, `service_layer/message_bus.py`, `unit_of_work.py`, `domain/commands.py`, `domain/events.py` | `nyxmon-agent/src/app.rs` (explicit wiring; no bus) |
| `startup_validation.py` | `nyxmon-agent/src/startup.rs` |
| `domain/models.py` | `nyxmon-core/src/model.rs` |
| `domain/*_config.py` | `nyxmon-core/src/config/{http,json_metrics,tcp,ping,dns,smtp,imap}.rs` |
| `service_layer/handlers.py` (`_should_notify_check_result`, helpers) | `nyxmon-core/src/decision.rs` |
| `service_layer/handlers.py` (`add_check_result`, `execute_checks`) | `nyxmon-agent/src/results.rs` |
| `service_layer/notification_policy.py` | `nyxmon-core/src/policy.rs` |
| `service_layer/notification_suppression.py` | rules/paths/freshness: `nyxmon-core/src/suppression.rs`; fetch: `nyxmon-checks/src/suppression_fetch.rs` |
| `service_layer/site_dependency.py` | `nyxmon-core/src/site/dependency.rs` |
| `adapters/site_connectivity.py` | pure: `nyxmon-core/src/site/{config,snapshot,observer,render}.rs`; I/O: `nyxmon-agent/src/site.rs` |
| `adapters/collector.py` | `nyxmon-agent/src/collector.rs`, `incidents.rs`; `estimated_check_runtime_seconds` in `nyxmon-core/src/lease.rs` |
| `adapters/cleaner.py` | `nyxmon-agent/src/cleaner.rs` |
| `adapters/notification.py` | `nyxmon-notify/src/{telegram,opsgate,logging,markdown,sanitize}.rs` |
| `adapters/repositories/interface.py`, `sqlite_repo.py` | `nyxmon-store/src/{actor,schema,checks,results,incidents,cleaner}.rs`; payload helpers (`carry_delivery_markers`, `with_delivery_intent`, `without_delivery_markers`) in `nyxmon-core/src/incident.rs` |
| `adapters/runner/async_runner.py`, `executors/__init__.py` | `nyxmon-agent/src/runner.rs`, `nyxmon-checks/src/registry.rs` |
| `executors/http_executor.py` | `nyxmon-checks/src/http.rs` |
| `executors/json_metrics_executor.py` | `nyxmon-checks/src/json_metrics.rs` (+ evaluation in `nyxmon-core/src/metrics_eval.rs`) |
| `executors/tcp_executor.py` | `nyxmon-checks/src/tcp.rs`, `starttls.rs`, `happy_eyeballs.rs` |
| `executors/dns_executor.py` | `nyxmon-checks/src/dns.rs` |
| `executors/ping_executor.py` | `nyxmon-checks/src/ping.rs` (+ parsers/command builder in `nyxmon-core/src/ping_text.rs`) |
| `executors/smtp_executor.py` | `nyxmon-checks/src/smtp.rs` |
| `executors/imap_executor.py` | `nyxmon-checks/src/imap.rs` |
| `executors/tls.py` | `nyxmon-checks/src/tls.rs` |

Below, the semantics that must be preserved, per area. "Fixture" means the
behaviour is pinned by a Python-generated fixture (section 7.2).

### 5.1 CLI, logging, startup (`cli.py`, `startup_validation.py`)

- Flags and defaults exactly as `start_agent`; `--log-level` choices
  `DEBUG INFO WARNING ERROR CRITICAL`. Additional Rust flags:
  `--check-schema`, `--shadow`, `--version`, subcommand `run-check`
  (section 7.4). Exit 1 when the DB file is missing
  (`Database file not found: …`), on schema failure, on lock contention, and on
  any fatal startup error (`Error running monitoring services`); exit 0 after a
  signal-initiated shutdown.
- Order: parse → logging → DB exists → open store → schema verification →
  agent lock → notifier selection (`Telegram notifications enabled`) → site
  config (`site connectivity observation is <mode>` when not off) →
  `validate_check_types` (logs only: `No checks found …`, or warnings listing
  unregistered types, or `Validated <n> check(s) - all types registered: …`) →
  install SIGTERM/SIGINT handlers **before** starting the collector (Python
  registers the receiver first, `run_monitoring_services`) → start collector,
  observer, cleaner → `Monitoring started with <n>s check interval`.
- No HTTP library request logging at INFO (`setup_logging` silences httpx);
  reqwest/hyper/rustls targets are set to WARN.

### 5.2 Notification policy (`notification_policy.py`) — fixture

- Resolution per severity: warning prefers `warning_*`, then unprefixed, then
  the global default; error uses unprefixed then global. `_bounded_int`
  rejects bools, non-ints (JSON `2.0` is a float → rejected), out-of-range;
  warn once per `(check_id, key)`; non-object `notification_policy` warns once
  under key `notification_policy`; `null` means "absent" (no warning).
- `global_reminder_seconds` checks the deprecated variable on every call
  (warning once per value).

### 5.3 Decision function (`handlers.py:_should_notify_check_result`) — fixture

Pure function `decide(state, check, result, now, cfg, snapshot, retry_seconds,
immediate_cooldown) -> (decision, annotated_result_data)`. Branch order is
part of the contract:

1. `status == ok` → no notify, `next = state.cleared()` (keeps `attempt_seq`).
2. status not error/warning → unchanged.
3. `force_notification` → notify, unchanged state (no attempt stamped).
4. truthy `collector_internal` → no notify, unchanged state.
5. truthy `notification_suppressed` → no notify, `with_streak_reset()` (keeps
   `last_immediate_at`, `held_since`, `attempt_seq`, `attempt_at`).
6. site: `dependency = resolve_site_dependency(check)` (always evaluated, so
   malformed declarations warn even when mode is off); with a snapshot:
   `hold = snapshot.hold_reason(dep, claim_started_at, now, stale_after)`,
   `recovered = snapshot.dependency_recovered(…)`; in `observe` mode with no
   hold, a non-null `observed_reason` annotates `site_connectivity={"held":
   false, …}`. `exhausted = held_since > 0 and now - held_since >= max_hold`.
   Hold and not exhausted → annotate `held: true`, no notify,
   `failure_count+1`, `first_failure_at or now`, `held_since or now`,
   `held=true`. Hold and exhausted → annotate `{"held": false, **hold,
   "exhausted": true}` and continue. `held_since := 0 if recovered else
   state.held_since`. `stale_after` and `max_hold` come from the observer's
   config, or the defaults when the feature is off (`_site_config`).
7. `retry_due = retry_seconds > 0 and attempt_at > 0 and now - attempt_at >= retry_seconds`.
8. truthy `notification_immediate` → notify if `retry_due or now -
   last_immediate_at >= cooldown`; `next = evolve(last_immediate_at=now if
   notify, held_since=held_since)`, then `_with_attempt` (`attempt_seq+1`,
   `attempt_at=now`) if notify. Failure streak untouched.
9. policy branch: `failure_count+1`; notify if `retry_due`, else if
   `last_notified_at == 0` → `failure_count >= consecutive_failures`, else
   `now - last_notified_at >= reminder_seconds`; next state evolves
   `failure_count, last_attempt_count (=failure_count if notify),
   last_notified_at (=now if notify), first_failure_at (or now), held_since`
   from the **read** state, then `_with_attempt` if notify.

`add_check_result` around it (`nyxmon-agent/src/results.rs`):
suppression details are computed once, only for error/warning results that
are neither forced nor `collector_internal`, and merged as
`notification_suppressed`; then up to `MAX_NOTIFICATION_STATE_ATTEMPTS = 3`
rounds of read state → decide → persist with transition; on the last
conflict persist without transition (passing `hold_marker = decision.now`
only when the decision held), never notify; `persisted == false` → never
notify. After a notify: `notifier.notify_check_failed`; if the outcome is not
`Failed` and `attempt_seq > 0`, acknowledge the attempt (errors logged, never
raised). Any error in this function is logged as
`result handling failed for check_id=<id>` and does not stop sibling results
(`handlers.py:execute_checks`).

### 5.4 Maintenance suppression (`notification_suppression.py`) — fixture + fake server

- Config object `notification_suppression`; empty/missing `url` → none.
  `timeout` via `_timeout_seconds` (non-numeric → 3, ≤ 0 → 3, cap 30).
- Fetch: GET with `Accept-Encoding: identity`, redirects followed by hand up
  to 5 hops (6 requests) on 301/302/303/307/308 with a `Location`; the
  `Authorization` (basic auth from `auth.username/password`, both strings)
  is sent on the first request and kept on a redirect hop when the target is
  same-origin (scheme, host, port) **or** an httpx "https upgrade"
  (`_is_https_redirect`: same host, `http` → `https`, ports default/80 →
  default/443), otherwise dropped, mirroring httpx `_redirect_headers`
  (which also replaces `Host` and rebuilds `Cookie` from the fetch's own
  jar, so a cookie set by a redirect hop is sent on the next hop); a hop
  without `auth=` uses credentials in its URL (`_build_request_auth`);
  redirect bodies are not read (the hand-written loop closes each redirect
  response); more than 5 redirects (`TooManyRedirects`), an invalid
  `Location`, any transport error, any non-2xx after redirects,
  `Content-Encoding` other than identity/empty (after `strip().lower()`),
  a declared (`int(Content-Length)`) or streamed body > 256 KiB and invalid
  JSON (`json.loads` on bytes, `pyjson`) → fail open (no payload).
- Implementation (S5b, `nyxmon-checks/src/suppression_fetch.rs`): one fresh
  `HttpSession` (the S5a client of 5.5) per fetch, so the cookie jar is the
  fetch's own and nothing is shared with the batch; every hop is one
  `HttpSession::send_once` with the extra header `Accept-Encoding: identity`
  and no automatic decompression (the client only decodes when asked to read
  a body, and the fetch refuses every encoding but identity before reading);
  redirects via `redirect_target`/`keeps_authorization`; the body is read
  with `read_body` under the 256 KiB cap. Each request uses the httpx
  timeout semantics of 5.5 (a NaN timeout fails before connecting, like
  `socket.settimeout(nan)`). The sync `httpx.Client` connects to the
  resolved addresses one after the other, while the session uses anyio's
  Happy Eyeballs: only the time to the same outcome differs (P2). Proxy
  variables are not read (D-15). Pinned by `suppression_fetch.json` (the
  Python `_fetch_payload` on an httpx `MockTransport`, replayed against a
  fake server: payload, request paths, `Authorization`, `Accept-Encoding`,
  `Cookie`); S9 joins it with S3's `suppression_request`/`evaluate`.
- Freshness guard, `active_statuses` (default `["running"]`, non-list →
  default), `status_path` (default `$.last_status`), `active_if` rules
  (`_rule_matches`: path must exist, `json_kind` equal, Python comparison,
  exceptions → no match), `active_for_seconds` with `finished_epoch_path`
  (default `$.last_run_finished_epoch`) and `0 <= now - finished <= active_for`;
  return dicts with the exact keys and `reason` defaults
  (`maintenance_active`, `maintenance_recently_finished`). `_int_or_none`
  follows Python `int()` on JSON values (bool → int, float truncates, numeric
  strings parse, inf/nan → none).
- Path lookup `_lookup`: `"$"` → payload; otherwise
  `path.replace("$.", "")` (every occurrence) split on `.`, empty parts
  dropped; dict key first, list index only for `isdigit()` parts.

### 5.5 HTTP family (`http_executor.py`, `json_metrics_executor.py`)

- One HTTP "session" per batch, dropped when the batch ends
  (`_async_run_batch` creates one `httpx.AsyncClient` per batch, shared by
  the http, json-http and json-metrics executors), **with one batch-scoped
  cookie jar**: httpx keeps one cookie jar per client, so a cookie set by
  one check's response (or redirect) is sent by later requests of other
  checks in the same batch to the matching domain; the `Cookie` header lists
  longer paths first like `http.cookiejar`.
- The session (`nyxmon-checks/src/http_client.rs`) is an HTTP/1.1 client on
  hyper's connection API, one connection per request, no proxy support
  (no proxy variable is set for the production agent); TCP connects use
  anyio's Happy Eyeballs (address order, 250 ms stagger) and the cookie jar
  matches the path as sent. URLs are parsed and
  joined like httpx (`httpx_url.rs`: `_urlparse`, `URL.join`/`urljoin`,
  credentials in the URL as basic auth), so the request target is httpx's
  `raw_path` (e.g. `%2e%2e` is not resolved, unlike a WHATWG parser);
  redirects and decoding are done by our own code to reproduce httpx:
  - redirects (`http_client.rs`, mirroring
    `AsyncClient._send_handling_redirects` and `_build_redirect_request`):
    only for 301/302/303/307/308 with a `Location`; URL joined against the
    request URL (fragment handling as httpx); method rules (303 → GET; a
    non-HEAD 301/302 → GET); headers per `_redirect_headers` (Authorization
    dropped unless same origin or https upgrade, `Host` replaced, `Cookie`
    rebuilt from the jar); at most 20 redirects, the 21st →
    `TooManyRedirects` → `request_error`; **each redirect response body is
    read completely before following** (httpx `await response.aread()`),
    under the read timeout, so a stalled redirect body yields `timeout` as in
    Python;
  - decoding: the request always sends `Accept-Encoding: gzip, deflate`;
    `Content-Encoding` values `gzip`, `deflate` (zlib-wrapped, falling back
    to raw deflate as httpx's `DeflateDecoder`), `identity`, and
    comma-separated chains are decoded by our streaming decoder; unknown
    encodings pass the body through undecoded (httpx behaviour); the
    declared `Content-Length` header stays visible for the json-metrics
    header cap, and the decoded-bytes cap applies to the decoder output.
- httpx timeout semantics: `timeout=t` bounds connect (incl. DNS), the TLS
  handshake, each read and each write separately — not the whole request.
  Rust: the socket is wrapped so every read and write that waits longer than
  `t` fails; no total timeout. An integer timeout beyond float range fails
  like httpcore (`OverflowError` → `unexpected_error`).
- Default request headers as httpx: `Accept: */*`,
  `Accept-Encoding: gzip, deflate`, `Connection: keep-alive`, and the
  User-Agent `python-httpx/0.28.1` (Q2). HTTP/1.1 only.
- The HTTP check never reads the final response body (`_fetch_headers`
  streams and closes); it does read redirect bodies when following (above).
- Error classification: httpx `TimeoutException` → a timed-out connect,
  handshake, read or write; `ConnectError` → DNS/TCP/TLS connect failures;
  other transport errors → `request_error`;
  URL build errors (`InvalidURL` is not a `RequestError` in httpx) →
  `unexpected_error`. IDN hosts (`https://home.wersdörfer.de` exists in
  production) are IDNA-encoded by both.
- Config parsing/validation exactly as `HttpCheckConfig.from_dict` +
  `validate` (types: `type(x) in (int, float)` excludes bool; bounds; messages)
  — fixture.
- json-metrics: shared client semantics (redirects followed), basic auth,
  status ≥ 400 → body not read; body cap on decompressed bytes; `json.loads`
  on bytes: UTF-8 with optional BOM (`utf-8-sig`) and UTF-16/32 by BOM or null-byte pattern, exactly as
  `json.detect_encoding` (P1, `pyjson`); `NaN`/`Infinity`/big integers as
  CPython (4.2); `retries` default **1**, `int()` on the raw value (strings,
  floats, bools accepted as Python does), `retry_delay` via `float()`;
  `validate` raises `TypeError` for string timeouts (→ `executor_exception`) —
  fixture. Path resolution with `[n]` → `.n` normalisation
  (`_resolve_path`), evaluation with `py_cmp`, failure records and summary as
  1.6 — fixture.

### 5.6 DNS (`dns_executor.py`)

Implemented in S5b as an own stub resolver (`nyxmon-checks/src/dns.rs`,
wire format and texts in `dns/wire.rs`, config in
`nyxmon-core/src/config/dns.rs`) that follows dnspython 2.8 directly; a
resolver library (the planned `hickory-resolver`) would hide the
resolution loop and the message handling the results depend on.

- `DnspythonResolver.query`: `Resolver()` reads `/etc/resolv.conf` on every
  check, also when `dns_server` is set (unreadable → `unexpected_error`
  "cannot open /etc/resolv.conf"; no `nameserver` line → "no nameservers";
  an entry that is not an IP address → the setter's `ValueError` →
  `configuration_error`); only `nameserver`, `options rotate` and
  `options edns0` matter (EDNS version 0 with payload 1232 only with
  `edns0`, otherwise no OPT record). A truthy `dns_server` replaces the
  list (a non-string such as `5` passes `validate` as an integer address
  and fails in the setter: `configuration_error`).
- `resolve(check.url, query_type, source=source_ip)` with `search=None`:
  `dns.name.from_text` (escapes, `EmptyLabel`/`LabelTooLong`/
  `NameTooLong`/`BadEscape` → `unexpected_error` with dnspython's text;
  non-ASCII → D-16), made absolute, no search list; `timeout` and
  `lifetime` = config timeout. The `_Resolution` loop: nameservers in
  order (shuffled with `rotate`), a backoff of 0.1 s doubling to 2 s before
  each further round, each attempt bounded by `min(lifetime - elapsed,
  timeout)`, `LifetimeTimeout` → `timeout` once the lifetime is used up;
  socket errors (`OSError`, e.g. a `source_ip` of the other family), `EOF`,
  unparseable or non-matching TCP responses, truncated TCP responses and
  rcodes other than NOERROR/NXDOMAIN/YXDOMAIN (SERVFAIL included, no
  `retry_servfail`) drop the nameserver; a timeout or another exception
  (e.g. an integer `source_ip`) keeps it; all dropped → `NoNameservers` →
  `unexpected_error` (P2 text); a truncated UDP response is retried once
  over TCP. UDP reads (unconnected socket) skip datagrams from other
  addresses, unparseable ones and those that are not a response to the
  query (`is_response`: QR, ID, opcode, question compared
  case-insensitively; FORMERR/SERVFAIL/NOTIMP/REFUSED without a question
  count as responses).
- Answer (`resolve_chaining`): the RRset of the query type for the
  response's question name, following CNAMEs in the answer section (at
  most 16, `ChainTooLong` drops the nameserver); duplicates within an RRset
  are dropped (rdata compared in canonical form); NXDOMAIN with an answer
  RRset (`AnswerForNXDOMAIN`) drops the nameserver, NXDOMAIN otherwise →
  `nxdomain` (also with CNAMEs); NOERROR without the RRset → `no_answer`;
  YXDOMAIN → `unexpected_error` (dnspython's text).
- Record texts as dnspython `to_text()`: A dotted quad; AAAA by
  `dns.ipv6.inet_ntoa` (longest zero run of at least two as `::`,
  `::a.b.c.d`/`::ffff:a.b.c.d` for the embedded IPv4 forms); MX `exchange`
  (preference dropped); CNAME/NS/PTR target; SOA `mname rname serial
  refresh retry expire minimum`; names with dnspython's escaping
  (`_escapify`) and a trailing dot, case as on the wire; TXT strings joined
  by a single space without quotes; a TXT string that is not UTF-8 raises
  `UnicodeDecodeError`, a `ValueError` → `configuration_error` with
  CPython's message.
- `questions`: `"<qname as queried, escaped, with trailing dot> IN
  <TYPE>"`; `response_code`: `"NOERROR"`; `dns_server`: the configured
  value or `"system"`; `source_address` when `source_ip` is truthy.
- Match: for A/AAAA compare `normalize_dns_value` (`ipaddress`
  `compressed` form, IPv4-mapped as `::ffff:a.b.c.d`) sets; otherwise exact
  string sets; any intersection → OK.
- Config validation (`from_dict` + `validate`, including the `TypeError`s
  of non-object data and unhashable `query_type`, which the executor
  reports as `unexpected_error` with `str(exc)`) and `expected_ip_error` —
  fixture `config_dns.json` (`cases`); the executor end to end —
  `config_dns.json` `executor_cases`: the real Python executor against a
  scripted DNS server (UDP/TCP), replayed by the Rust tests against the
  same script (results, key order, queries received); text helpers —
  `dns_text.json`. All four production DNS checks set `dns_server`
  explicitly.

### 5.7 SMTP (`smtp_executor.py`) — hand-rolled client, fake server tests

- Message: headers `From`, `To`, `Subject = "<prefix> <UTC ISO second>Z <token>"`
  (`token = token_hex(3)`), `Message-ID` with domain from the sender address
  (`parseaddr`, else `nyxmon.local`), `MIME-Version: 1.0`, `Content-Type:
  text/plain; charset="utf-8"`, `Content-Transfer-Encoding: 7bit`, body
  `Nyxmon SMTP health check. Correlation token: <token>. Safe to delete.`
  Header validation (`from_addr`, `to_addr`, `subject_prefix` non-empty, no
  CR/LF) — fixture.
- Dialogue as smtplib: connect (`implicit` → TLS first) with `timeout` per
  socket operation; greeting must be 220 else `SMTPConnectError`, which is a
  subclass of `SMTPResponseException` and is therefore caught by the
  response-error branch: a 4xx greeting (e.g. 421) → `temporary_failure`
  with `smtp_code` (retried), any other code → `smtp_error` with
  `smtp_code`; `EHLO <getfqdn()>` (smtplib default local
  hostname: `socket.getfqdn()` semantics); `starttls`: requires the
  `STARTTLS` extension (else `smtp_error` "STARTTLS extension not supported
  by server."), reply must be 220 (else response error), then EHLO again;
  `AUTH` when `username` set: password = `password` or `password_secret`
  (`get_password`); mechanisms in smtplib preference `CRAM-MD5`, `PLAIN`,
  `LOGIN` among the advertised ones, each tried in turn until one returns
  235 or 503, otherwise the last authentication error is raised
  (→ `auth_error`); no common mechanism → `smtp_error` "No suitable
  authentication method found."; no AUTH extension → `smtp_error` "SMTP AUTH
  extension not supported by server.".
- `send_message`: recipients from the `To` header (`getaddresses`); `MAIL
  FROM` refused → response error with code; all recipients refused →
  `smtp_error` with the Python repr of the refused dict; some refused →
  `recipient_refused` with the first refused code and message; `DATA`
  refused → response error. QUIT/close errors after DATA are ignored
  (delivery outcome preserved).
- Mapping order exactly as `SmtplibClient._send_blocking`'s `except` chain:
  auth error → `auth_error`; any `SMTPResponseException` (greeting via
  `SMTPConnectError`, `SMTPHeloError`, `SMTPSenderRefused`, `SMTPDataError`,
  STARTTLS reply) → `temporary_failure` (4xx) or `smtp_error`, with
  `smtp_code`; timeout → `timeout`/"connection timed out";
  `SMTPServerDisconnected` or connection refused → `connection_error`; other SMTP errors →
  `smtp_error`; TLS errors → `tls_error`; other OS errors →
  `connection_error`. Retries only for `temporary` (4xx), with `retry_delay`.
  `timeout` is reached only by the connect and the TLS handshake: a timed-out
  reply read is `SMTPServerDisconnected("Connection unexpectedly closed: timed
  out")` and a timed-out write `SMTPServerDisconnected("Server not
  connected")` in `smtplib`, so both are `connection_error`.
- Implementation notes (S7): `from_addr` and `to_addr` must be plain ASCII
  addresses (D-14); for those Python's email policy sends the header as
  configured and the envelope is the bare addresses, so no part of
  `email._header_value_parser` or `email._parseaddr` is ported. The fixture
  confirms byte parity of the flattened message, the envelope and the
  Message-ID domain for every corpus value Rust accepts. A `Subject` that is
  ASCII and fits one 78-character line is sent as it is (always the case
  with the default prefix); a longer or non-ASCII one becomes a series of
  RFC 2047 encoded words, one per line, decoding to exactly the subject
  (P2: Python folds it differently; wire bytes only, nothing stored).
  `Message-ID` is never folded. SMTPUTF8 is never needed. The EHLO name is
  `getfqdn()` via `gethostname` and `getnameinfo` (no `/etc/hosts` aliases),
  computed once per process.
  Retry delays go through an injected sleeper (`MailEnv`), so the tests
  record them instead of using a paused clock.

### 5.8 Collector (`collector.py`) — the core loop

One iteration (`_collect_once`), then `sleep(interval)` (interruptible by
stop), until stop:

1. First iteration only: close `collector:execution_paused`
   (`_reconcile_incidents_after_restart`, log when it existed) and warn about
   unobserved site requirements (`_warn_unobserved_site_requirements`).
2. Lease: `configured = processing_lease_seconds()`; every
   `max(5, min(30, configured/2))` s (monotonic) recompute
   `required = min(max(estimated_check_runtime_seconds(c) for enabled c), 3600)`
   with the exact estimator (`max_runtime_seconds`; ping formula; imap ×8,
   smtp ×5, tcp connect+handshake+timeout; defaults `timeout=10`,
   `retries=0`, `retry_delay=0` regardless of the executor defaults; numeric
   strings accepted; non-numeric warns once per `(check_id, field)`) —
   fixture; warnings on cap/extension once per value pair; on estimation
   error log and keep the cached value. `effective = max(configured, cached)`.
3. Stale recovery (uninterruptible by shutdown): up to 20 reclaim rounds
   until a round returns nothing (warning on the 20th); for each reclaimed,
   non-disabled check: `schedule_next_check`, persist the collector-internal
   result through the normal result path (`add_check_result`; never notifies,
   section 5.3 step 4); collect successes; then `_sync_stale_batch_incident`.
4. Stale batch incident (`collector:stale_processing_lease`): with
   recoveries → rehydrate retry marker from `delivery_pending`, claim with
   reminder 3600 (or 60 when the key is in the retry set) and the merged
   payload (`incident_type, reclaimed_count, iteration_count, check_ids
   (≤ 20, ints only), last_seen_at`), log, notify when granted, record send
   (failure → add to retry set; success → remove and acknowledge
   `delivery_attempt`). Without recoveries → if a row exists: rehydrate; if
   retry pending, claim without payload and resend (failed → keep open);
   close after 900 s quiet unless delivery is still pending. All errors logged.
5. Abandoned batch: if one exists and is unfinished →
   `_sync_paused_batch_incident` (claim `collector:execution_paused`, reminder
   3600/60, payload `incident_type, batch_size, check_ids (≤ 20)`, notify
   when granted with `_paused_batch_message`, record send) and **return**; if
   finished → clear it and close the incident (log).
6. If stop requested → return. Claim (uninterruptible); remember the claimed
   checks as the active batch; if stop requested now → return (they are
   released by shutdown).
7. If any: spawn the batch task; await it with deadline
   `effective + len(checks) * 60` s. Finished → active batch cleared.
   Deadline hit → active batch cleared, the still-running task becomes the
   abandoned batch (its checks remembered), error log line of 1.7. Only one
   batch runs at a time (Python `CapacityLimiter(1)`).

Collector incident notifications use the synthetic check
(`build_incident_notification`: `check_id=0`, `service_id=0`,
`check_type="internal"`, `url="internal://collector"`, data `error_type,
error_msg, incident_key, incident_alert_count, [opsgate_ticket=false]`) and
treat only `Failed` as undelivered; a send that panics/errors is `Failed`.
Names: `Nyxmon collector (stale check leases)`,
`Nyxmon collector (execution paused)`; error types
`stale_processing_lease_batch`, `collector_execution_paused`.

**Shutdown** (`_async_start` + `shutdown_deadline` + `request_stop`):

- SIGTERM/SIGINT → log `Received signal <n>, shutting down...` → request
  stop (no new claims).
- If at that moment an active batch exists or an abandoned batch is still
  running → wait up to `NYXMON_SHUTDOWN_GRACE_SECONDS` for it; otherwise
  cancel immediately. A batch finishing within the grace is persisted normally
  and cleared from the active set.
- When the grace ends with the batch still running, stop its intake
  (`IntakeStop.requested`): cancel the checks still running, persist (with
  normal notification) every result that finished before that point in order,
  then report `drained` without waiting for uninterruptible executors. Wait at
  most `SHUTDOWN_FLUSH_SECONDS` (15) for `drained`, else warn `results that
  finished during the shutdown grace period were not all recorded within 15s;
  releasing the remaining leases`; then abort (D-3). A batch that ended through
  the intake stop keeps its claims in the active set for the release.
- Stale recovery and the claim in progress complete before cancellation takes
  effect (Python shields them); everything else (sleeps, the paused-incident
  path, the observer, the cleaner) is cancelled at its next await.
- Then release owned claims of the active and the abandoned batch
  (section 3.4); failure → `failed to release check leases on shutdown`; exit 0.
- Stop the observer and cleaner tasks; no fixed process-level timeout (the
  unit's `TimeoutStopSec` is the backstop).

### 5.9 Runner (`async_runner.py`, `executors/__init__.py`)

All checks of a batch start concurrently; results are consumed serially in
completion order (3.7). Unknown types and executor panics produce the runner
results of 1.6. `validate_check_types` uses the same registered type list.

Implementation (S9a, `nyxmon-agent/src/runner.rs`): every check is a task
of a `JoinSet`, so dropping the batch future aborts the checks still running
(D-3). An exception escaping an executor (`Err(PyException)`) or a panic is
logged as `check executor raised unexpectedly for check_id=<id>
check_type=<t>` and becomes `executor_exception`; a check whose `data`
column cannot be decoded (`nyxmon_store::data_is_undecodable`) becomes
`{"error_type": "configuration_error", "error_msg": "check data is not
valid JSON"}` (D-8) before its executor runs. An intake stop is watched while
a result is being handled, as in Python: it aborts the running checks at
once, the results that had already finished are still handed over, then
`drained` is set. One `BatchCtx` (one HTTP session) is created per batch;
Python creates its client only for batches with HTTP-family checks, which is
not observable because the session opens no connection until used.

### 5.10 Cleaner (`cleaner.py`)

First cycle immediately at start, then every `--cleanup-interval` s; per
cycle up to 100 batches until a batch deletes fewer than `--batch-size`
rows; yield between batches; log totals and the not-drained warning;
`batch_size < 1` is rejected at startup (Python raises in the constructor →
exit 1).

### 5.11 Notifiers (`notification.py`) — fixtures + fake HTTP servers

- Telegram body exactly as `async_notify_check_failed` (header line
  `🔴 *Check Failed \(Critical\)*` or `⚠️ *Check Warning*`, `Name`, `URL`,
  optional `Status` (from `status_code`, truthy only), optional `Error Type`,
  `Error`, caps before escaping: name 200, url 500, error type 100,
  error message 2000 code points with `...[truncated N chars]`, `escape_markdown_v2`
  backslash first), OpsGate addenda (created / duplicate / error).
  `Name: Unnamed Check` when the name is empty.
- POST form-encoded `chat_id`, `text`, `disable_notification`
  (`"true"`/`"false"` as httpx renders bools), `parse_mode=MarkdownV2`,
  timeout 10 s (httpx semantics: connect plus TLS handshake within 10 s,
  then every single socket read and write within 10 s; reqwest's
  `read_timeout` cannot express this for the response head, so the
  notifiers use hyper's HTTP/1.1 client over a per-operation timeout
  wrapper); 2xx → delivered; HTTP 400 whose body
  contains `can't parse entities` (lowercased) → resend once as
  `markdown_v2_to_plain_text` without `parse_mode`; other failures → `Failed`
  with the redacted log line; no token/chat → `Unknown` with warning.
- OpsGate only when both base URL and token are set; `ticket_wanted` unless
  the internal row (`check_id == 0` and `check_type == "internal"`) carries
  `opsgate_ticket: false`; for errors, or warnings when
  `OPSGATE_SUBMIT_INCLUDE_WARNINGS`; POST JSON to `<base>/api/v1/tickets`
  with bearer token and the configured timeout; 201 + non-empty `id` →
  created; 409 + `error == "duplicate_open_ticket"` → duplicate; else error.
  Payload keys and order as `_build_opsgate_ticket_payload`; `expires_at`
  = `datetime.isoformat()` with microseconds (omitted when zero) and `Z`;
  `task_ref` rules of `_opsgate_task_ref`; prompt lines exactly as
  `_build_opsgate_prompt` with a fresh `NYXMON-UNTRUSTED-<16 hex>` boundary;
  `sanitize_untrusted_data` (200 code points, 25 items, depth 4, Cc/Cf/Zl/Zp →
  `?`); untrusted block `json.dumps(indent=2, sort_keys=True,
  ensure_ascii=True)` in which every backtick character is replaced by the
  six-character escape `\u0060` (backslash, `u`, `0060`), so endpoint text
  can never close the code fence — fixture with injected nonce, including
  `tests/unit/fixtures/opsgate_prompt_injection.json`, with injected clock.
  The request body is encoded like httpx `json=` (`ensure_ascii=False`,
  separators `,`/`:`, `allow_nan=False`: a NaN/infinite value makes the
  submit fail before sending → ticket status `error`). The OpsGate outcome
  never changes the delivery result.
- Logging notifier: returns `Delivered`; logs one line per alert at ERROR
  under `nyxmon.adapters.notification`; in `--shadow` mode the line is
  machine-readable: `notification check_id=<id> status=<s> error_type=<t>`
  (used by the shadow comparison).

### 5.12 TCP (`tcp_executor.py`)

- Host: `data.host` when truthy (any JSON type; not validated by Python),
  else `urlparse(url).hostname` (CPython 3.13 `urlsplit` rules, lowercased;
  its `ValueError` escapes the executor), else the raw url — fixture
  (`config_tcp.json`).
- Connect: Happy Eyeballs like `anyio.connect_tcp` (`ip_address(host)`
  connects directly, also for an integer host; otherwise getaddrinfo of
  `str(host)` with `SOCK_STREAM`, a non-ASCII name IDNA-encoded by anyio's
  `idna2008_resolve` = `idna.encode(host, uts46=True)`; order: the first IPv6
  address, then the first IPv4 address found after it, then the rest in
  resolver order; start the next attempt after 250 ms or as soon as the
  previous one failed; first success wins; when no attempt connects the
  error is always `OSError("All connection attempts failed")`, also for a
  single address; a resolver failure is the `socket.gaierror`
  `"[Errno N] <gai_strerror>"`; an IDNA error escapes as `unexpected_error`;
  a scoped IPv6 literal goes through the platform `getaddrinfo`, which on
  glibc accepts a zone only on link-local addresses, so `::1%lo` fails with
  "All connection attempts failed" on Linux in both implementations)
  bounded by `connect_timeout`; `connect_time_ms` from a monotonic clock,
  truncated.
- TLS: SNI/verification name = `sni or host`, IDNA-encoded by anyio's
  `idna2008_resolve` (IDNA 2008, not the IDNA 2003 of Python's `ssl` that
  the mail checks use); IP literals verify IP SANs without SNI; an empty
  name, a leading dot or a NUL fail like CPython's `ssl`; a name rustls
  cannot represent (not a valid DNS name, e.g. a scoped IPv6 literal or
  `127.0.0.1.`) is sent without SNI where OpenSSL would send the raw name
  and, when verifying, fails closed as `tls_error` before the handshake
  (P3, D-9);
  `verify=false` uses a no-verification rustls verifier;
  `tls_handshake_timeout` bounds the handshake; the STARTTLS dialogue is
  bounded separately by the same timeout. Dialogues (`generic`, `smtp`,
  `imap`, `sieve`) byte-exact (EHLO name `nyxmon.invalid`, IMAP tag `a1`,
  line cap 4096 bytes, dialogue cap 64 KiB, response truncation 500 chars,
  regexes, generic positive-response heuristic, pending-bytes check after
  the reply) — the classification helpers and scripted dialogues (the
  received chunks replayed through both implementations) are
  fixture-tested (`starttls_text.json`), the dialogues also fake-server
  tested.
- Certificate expiry from the peer leaf certificate even when not verifying:
  `floor((notAfter - now) / 86400)`; missing certificate → `cert_missing`.
- Closing (`finally: await current_stream.aclose()`): a TLS stream is closed
  with anyio's standard TLS closing handshake — send `close_notify`, then
  wait **without a time limit** for the peer's `close_notify`. A peer that
  closes or resets the connection without one (`BrokenResourceError`,
  `error_msg` `""`) or sends application data first (an IMAPS/SMTPS banner:
  OpenSSL `APPLICATION_DATA_AFTER_CLOSE_NOTIFY`) makes the close raise, which
  replaces the attempt's outcome (also `cert_expiry`/`cert_missing`) with
  `unexpected_error`, not retried. Verified against Python with local TLS
  servers; the production TCP checks (port 443 web servers) answer
  `close_notify`. A plain TCP stream closes without effect.

### 5.13 Ping (`ping_executor.py`)

- Configuration (`PingCheckConfig.from_dict` + `validate`: `float()`
  coercion, booleans rejected, whole-number `count`, check order and
  messages) and target normalisation (`normalize_ping_target`: URL host via
  `urlparse`, brackets, whitespace and leading `-` rejected) — fixture
  (`config_ping.json`).
- Literal pass-through: anything whose part before the first `%` is an IP
  address (also `192.0.2.1%x`) is used as is; otherwise
  `anyio.getaddrinfo(host, None, type=SOCK_DGRAM)` (a non-ASCII name is
  IDNA 2008 encoded first; the address text is `inet_ntop`'s, a non-zero
  IPv6 scope id appended as `%<id>`), first address in resolver order,
  bounded by `timeout` → `dns_timeout`/`dns_error`.
- Binary lookup: `shutil.which` on `PATH` (`CS_PATH` when unset, nothing
  for an empty `PATH`), then `/sbin/ping`, `/bin/ping`, `/usr/bin/ping`,
  `/usr/sbin/ping` (and `ping6` fallbacks), per check; command per
  platform exactly as `build_ping_command` (Linux:
  `ping -n -c 1 -W <max(1, ceil(timeout))> <addr>`); environment inherited
  plus `LC_ALL=C`, `LANG=C`; stdin null; output decoded with
  `errors="replace"`; killed after `timeout + 2` s.
- Per attempt: `interval` sleep between attempts; RTT parsing
  (`parse_rtt_ms` incl. the localized/TTL/source fallbacks; the four
  regexes are matched by hand with CPython `re` semantics: Unicode `\s`,
  `\w`/`\b` from the oracle's table `ping_unicode.json`, the `IGNORECASE`
  classes of the pattern letters, greedy backtracking order), permission
  markers, `_error_detail` (`str.splitlines`, `str.lower`), early stop on
  `permission_error`/`ping_unavailable`, all-timeouts message, packet stats
  with `round(…, 1)`, `rtt_avg_ms` from Python's compensated `sum` —
  fixture (`ping_text.json`, incl. scripted executor runs). Spawn errors
  render as `[Errno N] <strerror>: '<binary>'`: `ENOENT` →
  `ping_unavailable`, `EACCES`/`EPERM` → `permission_error` with
  `PERMISSION_HINT`, others → `execution_error`. A command line with a NUL
  character (an IPv6 zone id can carry one) and a resolver answer that is
  not an IP literal raise `ValueError` out of the executor
  (`executor_exception`), as in Python.
- Note: under the unit's `NoNewPrivileges=true`, file capabilities on
  `/usr/bin/ping` are not granted and `ping_group_range` is closed, so ping
  checks report `permission_error` with either agent (Q4). No production
  check uses ping today.

### 5.14 IMAP (`imap_executor.py`)

- Host: `data.host`, scheme stripped via `urlparse(...).hostname` when it
  contains `://` (`urlparse`'s `ValueError` → `unexpected_error`).
- Session: TCP connect with `timeout` (`socket.create_connection`, one
  socket timeout per operation); `implicit` → TLS first; `starttls` →
  require `STARTTLS` capability (imaplib `IMAP4.starttls` raises
  `abort("TLS not supported by server")`), and a non-OK reply raises
  `error("Couldn't establish TLS session")` — both are plain `imaplib` errors
  and therefore `unexpected_error`, not retried. The executor's
  `ImapTransientError("STARTTLS failed")` branch is unreachable with imaplib
  and is not reproduced as a reachable path; `none` → plain. Every protocol
  step bounded by `timeout` (timeout → `unexpected_error` "timed out").
  The TLS context (and the verify=false warning) is built per attempt, for
  `implicit` before connecting and for `starttls` after the greeting.
- Response handling as imaplib: a `BAD` completion raises
  `"<CMD> command error: BAD [<py repr of data>]"` → `unexpected_error`;
  a `NO` completion returns to `_run_checked` → `execution_error`; errors
  while waiting for a completion are prefixed `command: <CMD> => `
  (`socket error: EOF`, an untagged `BYE` text, `unexpected response: b'...'`).
- `LOGIN username password` uses `password` only — `password_secret` is not
  used for login (Python passes `config.password`, `None` when only the secret
  is set, which fails with `unexpected_error` "'NoneType' object has no
  attribute 'replace'" before LOGIN is sent). Parity kept; Q3 (owner
  decision). A rejected login raises `IMAP4.error(dat[-1].decode('UTF-8',
  'replace'))` → `unexpected_error` with the server text as a `str` (CPython
  3.13.16, e.g. `[AUTHENTICATIONFAILED] Authentication failed.`), not a
  bytes repr.
- `SELECT folder`; `UID SEARCH NOT DELETED HEADER SUBJECT "<subject>"`; per
  UID `UID FETCH <uid> (INTERNALDATE)` taking only the first response line
  whose `\bUID (\d+)\b` is that UID; skip older than `max_age_minutes`; sort
  ascending (stable); when `delete_after_check`: `UID STORE <uids> +FLAGS
  (\Deleted)`, then `CAPABILITY`, `UID EXPUNGE <uids>` only with `UIDPLUS`;
  `LOGOUT` best effort, also after a failed search/delete (not after a failed
  LOGIN/SELECT, which happen in `__aenter__`).
- Non-OK command → `execution_error` "<method> failed: <TYP> <py repr of
  data>"; TLS errors → `tls_error` (rustls texts, D-9); no match →
  `no_recent_message` (retried); retries only for no-recent, with
  `retry_delay`.
- Implementation notes (S7b): `nyxmon-checks/src/imap.rs` follows
  `imaplib.IMAP4` of CPython 3.13.16 step by step: tag prefix `Int2AP`, the
  tagged/untagged/continuation parser with literals, `_paren_depth` and blank
  line skipping, the untagged-response store (`OK`/`NO`/`BAD` cleared per
  command, response codes such as `CAPABILITY`, `READ-ONLY`), command states,
  `_check_bye`, `_astring`/`_quote` with ASCII encoding (`UnicodeEncodeError`
  text for non-ASCII values) and the NUL/CR/LF check, `_MAXLINE`. Python
  exceptions for non-string `username`/`password`/`folder` and numeric
  hosts are reproduced (`expected string or bytes-like object, got 'int'`,
  ...). `nyxmon-core/src/config/imap.rs` holds the configuration, host
  normalisation (`pyurl`), UID match, `Internaldate2tuple` + conversion and
  the `SEARCH` split. Pinned by `config_imap.json`, `imap_text.json` and
  `imap_dialogues.json`, the last being 46 dialogues of the real Python
  executor against scripted servers (Dovecot- and Gmail-like sessions,
  every error path above) replayed byte for byte by the Rust tests; TLS
  paths are Rust-only fake-server tests. Divergences: D-18.

---

## 6. Site connectivity in detail (`site_connectivity.py`, `site_dependency.py`)

The Rust port splits the observer into a pure state machine
(`nyxmon-core::site::Observer`) and an I/O driver
(`nyxmon-agent::site`). The pure machine takes `(now, probe outcomes,
store replies)` and emits store writes, notifications and log events; this is
what makes the Python-oracle replay fixtures (7.2) possible.

### 6.1 Configuration

`SiteConnectivityConfig.from_env` exactly (section 1.4). Endpoint parsing:
`host:port` / `[v6]:port` (`_split_endpoint`), IP literal of the right
family, port 1..65535, empty tokens skipped, an all-empty list is invalid;
explicitly empty variable → path unobserved. `stale_after = 3 ×
probe_interval`. `ProbeTarget` display `host:port` / `[v6]:port` / name.

### 6.2 Probes

One round probes every target of every observed path concurrently;
DNS targets succeed when `getaddrinfo(name, None)` returns any address; TCP
targets when a connection to the literal opens (then closed). Per-target
timeout `probe_timeout`; whole round bounded by `probe_timeout + 2` s;
unfinished targets count as failed. A path's round fails only when every
target failed. Errors are logged at DEBUG/ERROR and never raised.

### 6.3 Path state machine (`_apply_outcome`, `_advance_paths`)

States `up, failing, down, recovering, unobserved`; usable = `up, failing`;
blocking = `down, recovering`. Per round, outcome first:

| From | Round failed | Round ok |
| --- | --- | --- |
| up / failing | `failures += 1`, `failing`; at `failures >= down_after` → `down`, `down_since=now`, `recovered_at=release_at=0`, `failures=0` | `failures=0`; failing → up |
| down | stays down | → `recovering`, `recovered_at=now`, `release_at=now+grace` |
| recovering | → `down` (keeps `down_since`), `recovered_at=release_at=0` | stays |

Then, if `recovering` and `now >= release_at`: `last_release_at =
release_at`, → `up`, `release_at=0`, `failures=0`, and the release
`(path, last_release_at)` is reported to the phase machine. A failed round at
the release boundary therefore returns the path to `down` instead of
releasing it.

### 6.4 Phase machine (`_advance_incident`, `_close_incident`)

- `idle` and some path `down` → `active`, `incident_id = min(down_since)`,
  log opened.
- `active`: if no path is `down` → retire the undelivered ongoing intent of
  this incident (`retired: true`, never retried). If some path is blocking:
  releases this round → open/extend the recheck item
  (`{"pending": true, "release_at": max(previous if pending, max release)}`).
  If no path is blocking → close: participants = observed paths with
  `down_since >= incident_id > 0` and `recovered_at > 0`;
  `last_recovered_at = max(recovered_at)` (default now); `duration =
  max(last_recovered_at - incident_id, 0)`; `delivered` = ongoing delivered
  for this incident; eligible = `duration >= notify_after or delivered`;
  append a summary record once per `incident_id` (`incident_id, started_at,
  ended_at, paths{name: {down_since, recovered_at}}, ongoing_delivered,
  held_at_close (count_held_checks, 0 on error), attempt_at: 0,
  attempts: 0`); open the recheck with `release_at = max(round releases)`,
  else `max(last_release_at of participants)`, else now; log closed;
  `phase = idle`, `incident_id = 0`.

Round order (`run_once`): restore if needed (skip the round entirely when the
restore keeps failing) → `now` → probe → advance paths → `observed_at = now`
→ advance phase → publish snapshot → write payload → ongoing → summaries →
recheck.

### 6.5 Persisted payload, restore, writes

- Payload (`build_payload`), key order: `incident_type="site_connectivity"`,
  `version=1`, `phase`, `incident_id`, `observed_at`, `paths` (observed paths
  only: `state, down_since, recovered_at, release_at, last_release_at`),
  `ongoing`, `summaries`, `recheck`.
- Write (`_write_payload`): if the row is known present →
  `set_collector_incident_payload`; a missing row → `open_collector_incident`
  (creates it silently); any error → log, mark the row unknown, return false.
  The row is never deleted.
- Restore (`restore`): read failure → stay unrestored (no probe, no write,
  snapshot unchanged, warning each round); absent row → restored; unknown
  `version` → warning, ignore payload; per observed path restore valid states
  (`up/failing/down/recovering`) and timestamps (`_as_int`: bools and
  non-numbers → 0, floats truncated), `failures = 1` for a restored `failing`;
  restore `observed_at`, `phase`, `incident_id`, `ongoing`, `summaries`
  (dict records only), `recheck`. An `active` phase with a stale
  `observed_at` is downgraded: retire ongoing, every observed path → `up`
  with `down_since/recovered_at/release_at/failures` cleared (keeping
  `last_release_at`), `phase=idle`, `incident_id=0`, warning.

### 6.6 Ongoing alert (`_process_ongoing`)

Only in `active` with a `down` path and `now - incident_id >= notify_after`.
Same incident: delivered → wait for `reminder` since `last_alert_at`;
retired → wait for `reminder` since `max(last_alert_at, attempt_at)`;
otherwise wait 60 s since `attempt_at`. Then write the intent
`{incident_id, attempt_at: now, attempts: +1, delivered: false,
retired: false, last_alert_at}` **before** sending (abort the send if the
write fails), send `site_connectivity_outage` / status `error` /
ticket allowed with `_ongoing_message`, on delivery write
`delivered: true, last_alert_at: now`. Message:
`Site connectivity outage <id>: <path> down since <iso> (<dur>), … . Alerts of dependent checks are held for at most <dur(max_hold)>.`
with `_iso` (`%Y-%m-%dT%H:%M:%SZ`, `unknown` for ≤ 0) and `_duration`
(`Ns`, `MmSSs`, `HhMMm`).

### 6.7 Summaries (`_process_summaries`, `_summary_batch`, `_summary_line`)

Records sorted by `(started_at, incident_id)`; take records while each is due
(`attempt_at <= 0 or now - attempt_at >= 60`), at most 5 records and 3500
characters (lines joined by `\n`); a single over-long record has its path list
reduced from the end (`+N more path(s)`) and is hard-truncated as a last
resort. Mark the covered records `attempt_at=now, attempts+1`, write, send
`site_connectivity_summary` / status `warning` / `opsgate_ticket: false`,
on delivery remove exactly the covered records and write. Line text
`_render_summary_line`.

### 6.8 Recheck (`_process_recheck`)

Only in `enforce` with a pending item: count claims in flight before
`release_at` **first**, then list held checks; usable = held rows whose
dependency is fully usable on the current snapshot; reschedule usable rows
that are idle, enabled and due later than now to `now`; `pending = in_flight >
0 or usable non-empty`; when it changes, write `{pending, release_at}` and log.
Query or reschedule errors → log, keep pending.

### 6.9 Snapshot and hold logic (`SiteConnectivitySnapshot`)

Published after each round (and at construction/restore) as an immutable
value in a `tokio::sync::watch`/`ArcSwap`; the result handler reads it
without touching the store. `hold_reason` only in `enforce`;
`observed_reason` ignores the mode; both return none for unclassified checks
or a stale snapshot (`now - observed_at > stale_after`). `_reason`:
unmet requirement (observed members all blocking; all-unobserved groups are
met) → `{"reason": dependency_down|dependency_recovering, "paths": blocked in
declaration order, "down_since": min nonzero, "state": "down"|"recovering",
"incident_id"}`; otherwise with `claim_started_at > 0`, requirements whose
usable-since watermark (`min last_release_at` of currently usable observed
members) is later than the claim → `{"reason": "measured_before_release",
"paths": late, "down_since": 0, "state": "up", "incident_id", "release_at":
max watermark}`. `dependency_recovered` = fresh and no reason.
`dependency_usable` (recheck) = every requirement met.

### 6.10 Dependencies (`site_dependency.py`) — fixture

Shorthands `internet` (`[["dns"], ["ipv4","ipv6"]]`), `none`/`""`; object form
`requires` non-empty list of names or any-of lists (names trimmed and
lowercased, duplicates removed in order, unknown → invalid); invalid → warn once
per check and unclassified. `warn_unobserved_requirements`: single-name
requirements on unobserved paths, once per check.

### 6.11 Site messages

Through the process notifier with the synthetic row (name
`site connectivity`, incident key `site:connectivity`, `incident_alert_count`
0, `opsgate_ticket=false` for summaries) — OpsGate `task_ref`
`nyxmon-collector-site:connectivity`. Only `Failed` is a failed delivery.

### 6.12 Production note

Production runs with the feature `off`. Parity therefore rests on the replay
fixtures (7.2), the Rust unit/e2e tests, and the `observe`-mode shadow run
(9.1 phase B). Enabling the feature in production remains a separate owner
decision, unchanged by this port.

---

## 7. Testing strategy

### 7.1 Rust unit and integration tests

- `nyxmon-core`: exhaustive tests per module, driven mostly by fixtures.
- `nyxmon-store`: every operation against temporary databases built from
  `fixtures/schema/schema.sql`, mirroring `tests/adapters/test_sqlite_repo.py`,
  `test_collector_incident_store.py`, `test_collector_incident_delivery_ack.py`,
  `test_site_connectivity_persistence.py` (late completion cannot release a
  newer claim, deleted-check drops, CAS conflict rolls back result and check,
  conflict on reminder timestamps alone, FK cascade, bounded claims and
  reclaims oldest-first, legacy claim full lease, delivery markers carried and
  fenced, held marker fallback), plus a concurrency test with a second
  connection holding the write lock (busy timeout, `BEGIN IMMEDIATE`).
- `nyxmon-checks`: executors against local fake servers on `127.0.0.1`
  (tokio test servers): HTTP (status matrix, retries, redirects incl.
  `expected_location`, never reading the body, timeouts via a stalled
  server, gzip body cap), json-metrics, suppression (redirect auth, caps,
  compression refusal), TCP (plain, implicit TLS and STARTTLS for all four
  protocols with `rcgen` certificates, expiring/expired certificates, SNI
  override, rejection stages, over-long lines, pipelined data), SMTP (scripted
  fake server: success, 4xx retry, 5xx, auth failure, STARTTLS missing,
  partial/all recipients refused, QUIT error after DATA), IMAP (scripted fake
  server: search/fetch with unsolicited FETCH, UIDPLUS/no UIDPLUS, NO on
  SELECT, STARTTLS), DNS (a small UDP/TCP fake responder: A/AAAA/MX/TXT/SOA,
  CNAME chain, NXDOMAIN, empty answer, SERVFAIL, truncation → TCP), ping (a
  fake `ping` script on `PATH` printing canned iputils/macOS output, exit codes,
  sleep beyond the timeout, permission text).
- `nyxmon-notify`: fake Telegram and OpsGate servers (`can't parse entities`
  fallback, redaction, credential-less `Unknown`, ticket statuses).
- `nyxmon-agent`: collector tests with an injectable clock and fake executors
  mirroring `tests/unit/test_collector_recovery.py`,
  `test_collector_incidents.py`, `test_shutdown_claims.py`,
  `test_notification_*`, `test_site_connectivity_holds.py`.

The Python test files above are the checklist: every Python test that pins
agent behaviour gets a Rust counterpart or a fixture case; the mapping is kept
in `agent-rs/TESTMAP.md` (created in S1, filled by every slice) so reviewers
can see coverage per slice.

### 7.2 Shared fixtures (Python as the oracle)

`agent-rs/fixtures/generate.py` (run with `uv run python
agent-rs/fixtures/generate.py`, recipe `just rs-fixtures`) imports the real
Python modules and writes deterministic JSON files (sorted keys at the file
level, stable inputs, injected clocks/nonces/env via monkeypatching):

| Fixture | Python source | Content |
| --- | --- | --- |
| `pyjson.json` | `json.dumps`, `repr(float)`, `format(g)`, `round`, rich comparisons | value/op corpus incl. bool/int/float mixes, nested lists/dicts, unicode, control chars, astral chars |
| `env.json` | all env readers of 1.4 | raw value → parsed value + warning emitted |
| `config_<type>.json` | `*_config.py` `from_dict` + `validate` and the executors' pre-I/O steps | data corpus → `ok` (normalised config) / `configuration_error` message / Python exception class |
| `policy.json` | `resolve_notification_policy` | check data × status × env → policy + warnings |
| `site_dependency.json` | `resolve_site_dependency` | raw → requirements / unclassified + warning |
| `suppression_rules.json` | `_lookup`, `_rule_matches`, `_finite_seconds`, `_int_or_none`, and `notification_suppression_details` with a stubbed fetch | payload × config × now → details |
| `decision.json` | `_should_notify_check_result` with a stub UoW/snapshot | state × result × snapshot × env × now → decision + annotated data |
| `site_snapshot.json` | `SiteConnectivitySnapshot` methods | snapshot × dependency × claim × now |
| `site_replay/*.json` | `SiteConnectivityObserver` driven by a scripted probe runner, clock, in-memory store and recording notifier | per round: probe outcomes, store failures injected → payload after the round, snapshot, notifications, log events |
| `metrics_eval.json` (S5) | `JsonMetricsExecutor._evaluate`, `_resolve_path`, `_build_failure_summary`, `_bounded_actual` | payload × checks → failures/status/summary |
| `lease.json` | `estimated_check_runtime_seconds` | check data → seconds + warnings |
| `incident_payloads.json` | `_merge_stale_batch_payload`, `_stale_batch_message`, `_paused_batch_message`, delivery-marker helpers | |
| `telegram.json` | `escape_markdown_v2`, `markdown_v2_to_plain_text`, `_truncate`, the message builder of `async_notify_check_failed` (OpsGate stubbed) | |
| `opsgate.json` | `_build_opsgate_ticket_payload`, `_build_opsgate_prompt`, `sanitize_untrusted_data`, `_opsgate_task_ref` (nonce and time injected; incl. `tests/unit/fixtures/opsgate_prompt_injection.json`) | |
| `ping_text.json` | `build_ping_command`, `parse_rtt_ms`, `is_permission_failure`, `_error_detail`, `_packet_stats`, RTT statistics, output decoding, scripted executor runs (stub runner/resolver/sleep) | |
| `ping_unicode.json` | `re` `\w`/`\s`, `IGNORECASE` letter classes, `str.splitlines` separators | embedded `\w` table for the ping parsers |
| `starttls_text.json` | `_is_positive_starttls_response`, regexes, `_truncate` | |
| `dns_text.json` | `normalize_dns_value`, `expected_ip_error`, `_check_ip_match`, `dns.name.from_text`/`to_text`, `dns.ipv6.inet_ntoa`, UTF-8 decode errors | |
| `config_dns.json` `executor_cases` (S5b) | `DnsCheckExecutor` with dnspython against a scripted local DNS server | wire templates, results, queries received |
| `suppression_fetch.json` (S5b) | `_fetch_payload` on an httpx `MockTransport` | payload or failure, requests |
| `smtp_text.json` | `_build_subject` (token/time injected), `_message_id_domain`, header validation | |
| `schema/` | Django migrate | section 2.4 |

Normalisation rules applied by the generator (and identically by the drift
test) before writing:

- messages embedding one of the known constant sets (`ALLOWED_TLS_MODES`,
  `ALLOWED_NO_RECENT_MESSAGE_SEVERITIES`, `ALLOWED_OPERATORS`,
  `ALLOWED_SEVERITIES`, `VALID_QUERY_TYPES`) have the Python set repr
  replaced by the sorted rendering (`{'a', 'b'}`), so the stored expectation
  is independent of `PYTHONHASHSEED` and equals the Rust output exactly; the
  generator runs with `PYTHONHASHSEED=0` as a second safeguard;
- clocks (`time.time`, `datetime.now`), `secrets.token_hex`, and the
  observer clock are injected; library-derived (P2) messages are not
  recorded (the generator stores `{"error_type": …, "error_msg_class": "P2"}`
  for them and the Rust test checks only the error type).

Drift guard: `tests/unit/test_agent_rs_fixtures.py` regenerates every file
in memory and asserts equality with the committed version, so a Python
behaviour change cannot merge without regenerating fixtures, and the Rust
tests then show what the Rust side must change. Rust loads fixtures with
`include_str!` from `agent-rs/fixtures`.

### 7.3 Process-level e2e (Rust binary)

`agent-rs/crates/nyxmon-agent/tests/`: start the built binary against a temp
database (from the schema fixture) and fake servers:

- SIGTERM/SIGINT during an in-flight check releases the claim without result
  or alert (`tests/e2e/test_graceful_shutdown.py` analogues), a result
  finishing within the grace is recorded, idle shutdown is prompt;
- SIGKILL leaves the claim; the next start reclaims after the lease, records
  `stale_processing_lease` and sends exactly one stale-batch alert (logging
  notifier in `--shadow`-style machine format);
- lock contention exits 1; old schema exits 1; missing DB exits 1;
- a site observer run with fake probe targets (`127.0.0.1:<closed port>` /
  `127.0.0.1:<open port>`) writes the expected payload sequence.

### 7.4 Offline parity harness (Python vs Rust on identical inputs)

`agent-rs/parity/run_parity.py` (`just rs-parity`): builds two copies of a
database seeded with a curated check corpus (every check type, policies,
suppression, unknown type, disabled checks; short `check_interval`s) whose
targets are deterministic local fake servers (scripted per-request
responses: flapping HTTP, metrics thresholds, TCP/TLS with a near-expiry
certificate, DNS fake responder, SMTP/IMAP fakes, fake `ping` on `PATH`).
It runs `start-agent` on copy A and `nyxmon-agent` on copy B concurrently,
each against its **own** set of fake servers, then compares.

Determinism without injecting a clock into either process:

- fake servers script responses **by request index per check endpoint**
  (n-th request to `/check/17` → response n), never by wall time, so the
  n-th sample of a check sees the same response in both runs regardless of
  scheduling jitter; the comparison aligns samples by index;
- check intervals (2–5 s) and policy windows (`reminder_seconds` and
  cooldowns far longer than the run, `consecutive_failures` thresholds,
  `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS` unset) are chosen so that every
  notification decision depends only on sample order, not elapsed time;
  time-based paths (reminders, retry, holds, incident reminders) are covered
  by the fixtures and Rust unit tests with injected clocks instead;
- sample boundaries: every fake endpoint serves a finite script of N
  responses whose last response is OK, and repeats that OK response for
  every further request; an OK sample never notifies and only clears state,
  so samples after N cannot add a notification decision. The harness stops
  each agent (SIGTERM) only after every **enabled** check has at least N
  result rows in that agent's database (with an overall timeout that fails
  the run), and compares **only the first N samples per check**; disabled
  checks of the corpus are asserted separately to have zero result rows and
  to stay `idle` in both databases;
- state per sample: a read-only poller (200 ms; check intervals are ≥ 2 s)
  records, per check and database, the `check_notification_state` row
  observed while the check has exactly k result rows, for k = 1..N; the
  persist transaction writes the result and the state together
  (section 3.4), so each observation is consistent with sample k;
- compared per check: the ordered list of the first N `(status,
  error_type, data key list, nyxmon-authored values)`; the per-sample state
  rows on `failure_count`, `last_attempt_count`, `attempt_seq`, and zero/non-zero
  of every timestamp column **except `attempt_at`**, which the delivery
  acknowledgement clears in a separate transaction after the send, so a
  poll at sample k may legitimately see either value (its transitions are
  pinned by the decision fixtures and the Rust store/handler tests instead;
  the harness only checks that `attempt_at` is 0 in both databases after
  the final OK sample, which clears it); notifications **per check** as the ordered
  list of `(status, error_type)` from the Python `LoggingNotifier` lines and
  the Rust machine lines (no cross-check ordering is compared, because
  concurrent checks may complete in a different order);
  `collector_incident` on key, `alert_count`, payload keys and counters at
  the end of the run;
- masked: P2 fields, timestamps and durations (`created_at`, `*_at`,
  `*_ms`, `latest_internaldate`), and random values (D-12: SMTP `subject`
  suffix and `token`, OpsGate boundary).

The harness is a CI job (section 7.5) with a ~2 minute run; "zero unmasked
differences" is its gate.

`nyxmon-agent run-check --db PATH --check-id N [--imap-no-delete]` executes
one check from a database opened read-only and prints the result JSON
(no persistence, no notification, no lock). It is used by the harness for
single-check comparisons and on macmini for the mail checks (9.1 phase C).

### 7.5 CI and just recipes

- `.github/workflows/ci.yml` gains a `rust` job (pinned action SHAs like the
  existing jobs; toolchain from `rust-toolchain.toml`; cargo cache):
  `cargo fmt --all --check`, `cargo clippy --workspace --all-targets --locked -- -D warnings`,
  `cargo test --workspace --locked`, `cargo build --release --locked` with a
  binary-size report. A `parity` job (needs `python` and `rust`) runs
  `just rs-parity`. The existing `python` job runs the fixture and schema
  drift tests as part of `pytest`.
- justfile:
  - `test` → `uv run pytest` then `just rs-test`;
  - `lint` → pre-commit then `just rs-lint` (`fmt --check` + `clippy -D warnings`);
  - `typecheck` → `uv run mypy` then `cargo check --workspace --all-targets --locked`;
  - `rs-test`, `rs-lint`, `rs-fmt`, `rs-fixtures`, `rs-parity`,
    `rs-build` (host), `rs-build-linux` (Docker `--platform linux/amd64`
    with a pinned `rust:<ver>-bookworm` image, glibc 2.36 ≤ host 2.39, for the
    local-archive deploy path).
- `AGENTS.md`/`CLAUDE.md` gain the Rust commands.

---

## 8. Build, release, deployment

### 8.1 Target triple (decision): `x86_64-unknown-linux-gnu`, not musl

The brief lists a static musl binary as a goal. Decision: ship a glibc
binary built against an old glibc floor (2.35), TLS via rustls (no OpenSSL),
so the only dynamic dependencies are glibc/libgcc. Reason: name resolution
parity. Every HTTP, TCP, SMTP, IMAP, probe and ping target is resolved through
`getaddrinfo` in Python (glibc + `nsswitch.conf`). macmini's resolver
configuration is `hosts: files mdns4_minimal [NOTFOUND=return] dns` with two
nameservers (`127.0.0.1`, `192.168.178.94`). musl's resolver ignores
`nsswitch.conf` and queries all nameservers **in parallel, taking the first
answer**, while glibc asks them in order. With split-horizon names
(`*.home.xn--wersdrfer-47a.de`, Tailscale `*.ts.net` names used by 20+
production checks) two nameservers can legitimately answer differently, so
musl would change which address a check reaches. A glibc build removes that
whole risk class; the binary is still a single self-contained file plus libc.
`objdump -T` in CI asserts no symbol newer than `GLIBC_2.35`. A musl build can
be added later if the owner prefers static linking and accepts the resolver
difference (Q1).

### 8.2 Release pipeline (decision): GitHub Actions release artifact

- Workflow `.github/workflows/agent-rs-release.yml`, triggered by tags
  `agent-rs-v<semver>` (and `workflow_dispatch` producing a build artifact
  without a release). Runs on `ubuntu-22.04` (glibc 2.35), full test suite,
  `cargo build --release --locked --target x86_64-unknown-linux-gnu`, GLIBC
  floor check, `nyxmon-agent --version` smoke test, packs
  `nyxmon-agent-<ver>-x86_64-unknown-linux-gnu.tar.gz` (binary + LICENSE)
  and `<archive>.sha256`, creates the GitHub release with both files
  (`permissions: contents: write` only in this job).
- The tag must equal the version in `crates/nyxmon-agent/Cargo.toml`
  (checked). `nyxmon-agent --version` prints version and git commit.
- Rejected alternatives: building on macmini (puts a Rust toolchain on the
  production host, slow, non-reproducible); local build + rsync as the main
  path (unreproducible artifacts, Docker emulation on an ARM laptop). Local
  builds remain available for development (`nyxmon_agent_rust_local_archive`).

### 8.3 ops-library `nyxmon_deploy` role changes

New defaults (`defaults/main.yml`):

```yaml
nyxmon_agent_impl: python                 # python | rust
nyxmon_agent_rust_version: ""             # e.g. "0.1.0"; required for impl=rust
nyxmon_agent_rust_sha256: ""              # sha256 of the release archive; required with a version
nyxmon_agent_rust_release_url: >-
  https://github.com/ephes/nyxmon/releases/download/agent-rs-v{{ nyxmon_agent_rust_version }}/nyxmon-agent-{{ nyxmon_agent_rust_version }}-x86_64-unknown-linux-gnu.tar.gz
nyxmon_agent_rust_local_archive: ""       # controller path; overrides the download (development)
nyxmon_agent_rust_install_root: /opt/nyxmon-agent
nyxmon_agent_rust_keep_versions: 3       # never prunes the active or the previous version
```

New `tasks/agent_rust.yml`, included from `main.yml` after `django.yml`
(migrations applied) and before `monitoring.yml`:

1. Assert `nyxmon_agent_impl in ['python', 'rust']`; for `rust` assert a
   version with a 64-hex sha256, or a local archive plus a version.
2. When a version is set — independent of the impl, so the binary can be
   installed ahead of the switch — stage the candidate: download with
   `get_url` + `checksum: sha256:…` (or `copy` the local archive) and unpack
   into `{{ install_root }}/{{ version }}/` (root:root, 0755). An existing
   version directory is never modified in place (same version, same
   checksum); a different archive for an existing version fails the run.
3. Validate the candidate **by its versioned path**, before anything points
   at it, as the service user: `<versioned>/nyxmon-agent --version` must
   report the requested version, and `<versioned>/nyxmon-agent --check-schema
   --db <db>` must succeed. A failure stops the play; the running unit, the
   drop-in and every other installed version are untouched.
4. Drop-in `/etc/systemd/system/nyxmon-monitor.service.d/agent-impl.conf`,
   present iff `impl == rust`, naming the **versioned** binary (no `current`
   symlink):

   ```ini
   [Service]
   ExecStart=
   ExecStart=/opt/nyxmon-agent/<version>/nyxmon-agent --db {{ nyxmon_django_path }}/db.sqlite3 --enable-telegram
   ```

   (plus `Environment=MALLOC_ARENA_MAX=2` only if 4.4 requires it); absent for
   `python`. Because the version is part of the drop-in, every version change
   and every impl flip changes the file and notifies `reload systemd` and
   `restart nyxmon-monitor`; a deploy that changes nothing restarts nothing.
   The base unit template (`nyxmon-monitor.service.j2`) is unchanged, so its
   Python `ExecStart`, hardening, `EnvironmentFile`, `UMask` drop-in and
   `ReadWritePaths` apply to both implementations.
5. After handlers flushed: verify the running implementation from the live
   process, retrying for up to 30 s like `validate_opsgate_smoke.yml`: unit
   active, `MainPID > 0`, and
   - `rust`: `readlink /proc/<pid>/exe` equals
     `{{ install_root }}/{{ version }}/nyxmon-agent` exactly;
   - `python`: `/proc/<pid>/cmdline` argv contains
     `{{ nyxmon_venv_bin }}/start-agent` (the uv venv interpreter is a symlink
     to a uv-managed Python outside the venv, so `/proc/<pid>/exe` cannot
     identify the venv).
6. Only after step 5 succeeded: prune installed versions beyond
   `keep_versions`, never removing the version in the drop-in, the
   previous version (rollback target), or anything when the run failed.

The Python venv keeps being built for both implementations (the dashboard
needs it and it is the rollback path). README (variables, switch, rollback,
lock file), CHANGELOG entry and a minor version bump of the collection;
the molecule scenario converges with `impl: rust` from a local archive built
in CI when feasible, else asserts the drop-in rendering only.

### 8.4 ops-control changes

- `inventories/prod/host_vars/macmini.yml`: `nyxmon_agent_impl`,
  `nyxmon_agent_rust_version`, `nyxmon_agent_rust_sha256` (role defaults <
  host vars; not set in the playbook's role vars so the inventory is the single
  place to flip).
- `playbooks/deploy-nyxmon.yml`: pre-task asserting the ops-library version
  that knows `nyxmon_agent_impl` (existing pattern for 2.31.29).
- `docs/NYXMON_RUST_AGENT.md`: shadow, cutover, verification, rollback
  runbook; CHANGELOG entry.

### 8.5 Rollback

- Normal: set `nyxmon_agent_impl: python` in macmini host vars and run
  `just deploy-one nyxmon` (in tmux). The role removes the drop-in, systemd
  restarts the unit with the venv `start-agent`; SIGTERM makes the Rust agent
  release its leases; the Python agent takes the lock and continues from the
  same database. Nothing in the database depends on which agent wrote it.
- Emergency without Ansible (operator on the host):
  `rm /etc/systemd/system/nyxmon-monitor.service.d/agent-impl.conf && systemctl daemon-reload && systemctl restart nyxmon-monitor`,
  then flip the inventory variable before the next deploy (the next run with
  `rust` would reinstate the drop-in).
- Forward again: flip back to `rust`; the installed binary is reused.
- Rust-to-Rust rollback (bad release): set `nyxmon_agent_rust_version` and
  `…_sha256` back to the previous release; the versioned directory is still
  installed (step 6 keeps it), the drop-in changes, the unit restarts.

### 8.6 Documentation

nyxmon: `agent-rs/README.md` (build, test, run), `docs/agent-rs.md`
(architecture, schema contract rule of 2.3, parity classes, divergences,
operations), updates to `docs/configuration.md` (both implementations read
the same variables; lock file; `--check-schema`, `--shadow`, `run-check`),
`docs/concurrency-overview.md` (Rust runtime section), `docs/index.md`,
`CHANGELOG.md` per user-visible slice. ops-library and ops-control as above.

---

## 9. Rollout

### 9.1 Shadow runs on macmini (no production writes, no Telegram)

Prerequisites: S1–S13 merged, release `agent-rs-v0.1.0` published, the role
deployed with `nyxmon_agent_impl: python` and the version pinned (binary
installed, nothing switched; S14).

Phase A — default mode, 24 h:

1. Copy the live database online:
   `sqlite3 /home/nyxmon/site/db.sqlite3 ".backup /var/lib/nyxmon-shadow/db.sqlite3"`
   (directory owned by `nyxmon`, 0700). Note the copy time T0.
2. In the copy only: disable `smtp` and `imap` checks (`UPDATE health_check
   SET disabled=1 WHERE check_type IN ('smtp','imap')`) — a shadow IMAP check
   would delete the messages the production IMAP check expects
   (`delete_after_check`), and a shadow SMTP check sends extra mail; release
   the claims the copy inherited from the production process, which can
   never complete them there (`UPDATE health_check SET status='idle',
   processing_started_at=0 WHERE status='processing'`, keeping
   `next_check_time`), so they do not turn into shadow stale-lease results;
   delete `collector_incident` rows so shadow incidents start clean.
   Record the copy's `check_notification_state` as the baseline B0.
3. Run in tmux as a transient unit with the production hardening:
   `systemd-run --unit nyxmon-agent-shadow -p User=nyxmon -p Group=nyxmon -p NoNewPrivileges=yes -p ProtectSystem=strict -p ReadWritePaths=/var/lib/nyxmon-shadow -p EnvironmentFile=<shadow env with NYXMON_* only> /opt/nyxmon-agent/<version>/nyxmon-agent --db /var/lib/nyxmon-shadow/db.sqlite3 --shadow`
   and follow `journalctl -fu nyxmon-agent-shadow`.
4. History sources, because `check_notification_state` keeps only the
   latest state and `check_result` only 24 h:
   - `agent-rs/parity/shadow_record.py`, started together with the shadow
     unit in the same tmux session, opens both databases read-only
     (`mode=ro`) every 60 s and appends to a JSONL file, per database: the
     new `check_result` rows (id > last seen); every changed
     `check_notification_state` / `collector_incident` row; the lease state
     of every `health_check` row (`id, status, processing_started_at,
     next_check_time, disabled`); and retention counters (`count(*)`,
     `min(created_at)`, `min(id)` of `check_result`) — so stuck claims and
     cleaner deletions are verifiable from the record;
   - `attempt_seq` is a monotonic per-check counter incremented by every
     notification decision of either branch (policy and
     `notification_immediate`, `handlers.py:_with_attempt`) and never reset,
     so `attempt_seq(end) - attempt_seq(T0)` is the exact number of alerts
     decided per check in each database, independent of the sampling;
   - the shadow notifier's machine lines (5.11) give the alert timeline of
     the shadow side; the production side's timeline comes from the
     recorder's `attempt_seq` changes.
   Then `agent-rs/parity/shadow_compare.py --record <jsonl> --since T0`
   reports per enabled non-mail check: statuses and error types over the
   window (aligned by time, with both samples' timestamps for every
   disagreement), data key sets per `(status, error_type)`, alert counts and
   times, stale leases (no `stale_processing_lease` result in the shadow
   database), claims stuck in `processing` (no recorded lease snapshot with
   `processing_started_at` older than the effective lease), cleaner
   deletions (the shadow database's `min(created_at)` stays within the
   retention period plus one cleanup interval after the first 24 h and
   `min(id)` advances), and RSS/CPU (`systemctl show -p MemoryPeak`, `ps`).
   At the end of the window the same lease and retention queries are also
   run once directly against both databases (read-only) as a cross-check of
   the record.
5. Acceptance: no unexplained difference in error types or key sets; status
   disagreements only where the endpoint itself changed between the two
   samples (the harness reports both samples' timestamps so each disagreement
   can be explained); no extra or missing notification decisions beyond those
   explained by such sample-time differences; RSS/size targets of 4.4 met.
   Findings are fixed in a slice and phase A repeated (shorter) if code changed.

Phase B — `NYXMON_SITE_CONNECTIVITY_MODE=observe`, ≥ 4 h, same setup: the
`site:connectivity` row of the copy evolves (`observed_at` advances every
probe interval, paths `up`), no messages other than log lines. Optionally a
controlled outage drill is not run on the production host (it would affect
the production agent); site outage behaviour is covered by fixtures and e2e.

Phase C — mail executors: `nyxmon-agent run-check --db <copy> --check-id <smtp id>`
and `run-check --imap-no-delete --check-id <imap id>` once each, compared
field-by-field with the latest production results of the same checks
(`smtp` sends one extra test mail, which the production IMAP check then
consumes — harmless).

Stop and remove the shadow unit and copy afterwards.

### 9.2 Cutover

1. Announce a window; the owner approves the deploy (it restarts the
   monitoring unit).
2. Set `nyxmon_agent_impl: rust` in macmini host vars; `just deploy-one nyxmon`
   in tmux. The role installs nothing new (binary present), runs migrations,
   `--check-schema`, writes the drop-in, restarts `nyxmon-monitor`, verifies
   `/proc/<pid>/exe`, runs the OpsGate env smoke check
   (`validate_opsgate_smoke.yml`, which reads `/proc/<pid>/environ` and works
   for either implementation).
3. Record cutover time T1.

### 9.3 Verification after cutover

- `systemctl status nyxmon-monitor`: active, MainPID exe under
  `/opt/nyxmon-agent/<ver>/`, journal shows the startup lines of 1.7 with
  `Validated 76 check(s) …`.
- Every enabled check has a new `check_result` row with `created_at > T1`
  within `check_interval + 60 s` (SQL query in the runbook; the hourly checks
  bound the wait to ~61 min), and the status distribution matches the last
  hour before T1.
- No `health_check` row stays `processing` longer than the effective lease;
  no `stale_processing_lease` results after T1.
- Dashboard shows fresh results (spot check two checks per type).
- Notifications: with owner consent, a controlled failing check (temporary
  `tcp` check against a closed local port with
  `notification_policy.consecutive_failures: 1`) produces exactly one
  Telegram message with the expected format and, as an error, one OpsGate
  ticket (or the duplicate notice); then the check is deleted and its OpsGate
  ticket closed. Alternatively wait for a natural alert during the soak.
- Cleaner ran (`Cleaned up …` or no-op) and `check_result` count stays at the
  24 h retention level.
- RSS within 4.4 after one hour and after 24 h.
- Rollback drill: flip to `python`, deploy, confirm the Python agent resumes
  (new rows, no stale leases), flip back to `rust`, deploy, re-verify. Two
  restarts, proves 8.5 end-to-end.

### 9.4 Soak and rollback criteria

Soak 7 days with daily checks of the 9.3 queries. Roll back immediately on:
a check without new results for `2 × interval + lease`, stuck claims, any
missing or malformed Telegram alert for a real incident, a crash loop
(`NRestarts > 0`), an error-type shift without an endpoint explanation, or
RSS above budget trend. After the soak the owner decides when to retire the
Python agent (Q6).

---

## 10. Implementation slices

Rules: one branch per slice (`rust-agent/sNN-<name>`), reviewed by
`gpt-6.1-sol` (high) via the codex review loop, merged to `main` and pushed.
Merging Rust code to `main` is safe at any time because nothing deploys
`agent-rs/` until the role is switched (rsync and git deploys ship
`src/django`, `src/nyxmon`, `src/nyxboard` only; the role ignores
`agent-rs/` until S14). Every slice keeps `just test`, `just lint`,
`just typecheck` green, updates `agent-rs/TESTMAP.md`, and lists the Python
functions it ports in its review context. Target diff size ≤ 2.5k lines
including tests; fixtures (generated JSON) are excluded from that budget but
reviewed by spot checks.

| # | Slice | Scope | Depends on | Parallel with |
| --- | --- | --- | --- | --- |
| S1 | Foundations | workspace, toolchain pin, CI `rust` job, just recipes, `pyjson`, `env` readers, domain types, `tls.rs` helpers, fixture generator skeleton + drift test, `TESTMAP.md`, `agent-rs/README.md` | — | — |
| S2 | Store | `nyxmon-store` (actor, all ops of 3.4, schema verification, `--check-schema` core), schema fixtures + contract test, agent lock in Rust **and** `cli.py:start_agent` (Python test) | S1 | S3, S5–S8 |
| S3 | Alerting core | policy, site dependency, site snapshot + hold logic, suppression rules/paths/freshness, decision function, incident payload helpers, lease estimator; fixtures | S1 | S2, S5–S8 |
| S4 | Site observer core | pure observer (6.1, 6.3–6.9), payload codec, restore, rendering; replay fixtures | S3 | S2, S5–S8 |
| S5 | HTTP family + DNS | http/json-http, json-metrics (incl. its pure evaluation `metrics_eval.rs`), the shared redirect/decoding/cookie layer, suppression fetch (payload only; rule evaluation is S3, joined in S9), DNS executor; configs + fixtures; fake servers | S1 | S2–S4, S6–S8 |
| S6 | TCP + ping | happy eyeballs, TLS/STARTTLS dialogues, cert expiry, ping subprocess/parsers; fixtures; fake servers | S1 | S2–S5, S7, S8 |
| S7 | SMTP + IMAP | hand-rolled SMTP client, hand-rolled `imaplib` emulation incl. STARTTLS (5.14); configs; fake servers | S1 | S2–S6, S8 |
| S8 | Notifiers | Telegram (escape, fallback, redaction), OpsGate (payload, prompt, sanitize), logging/shadow notifier; fixtures; fake servers | S1 | S2–S7 |
| S9 | Agent runtime core | CLI/logging/startup (1.7, 5.1), runner, result handler (5.3), basic collector loop (claim/execute/sleep), cleaner, signals and graceful shutdown with release (5.8 shutdown part), `--shadow` | S2, S3, S5–S8 | — |
| S10 | Collector reliability | stale reclaim + results + stale-batch incident, lease estimation wiring, batch deadline/abandon/paused incident, incident delivery retry/ack, restart reconciliation | S9 | S11, S13 |
| S11 | Site observer runtime | probe runner, observer task, snapshot sharing, recheck/store wiring, site notifier, unobserved warnings; e2e with local probe targets | S4, S9 | S10, S13 |
| S12 | Parity + e2e + docs | process e2e (7.3), offline parity harness + CI job (7.4), `run-check`, `shadow_compare.py`, nyxmon docs (8.6) | S10, S11 | S13 |
| S13 | Release pipeline | release workflow, GLIBC floor check, version check; first tag `agent-rs-v0.1.0` after S12 | S9 (workflow), S12 (tag) | S10–S12 |
| S14 | Deployment role | ops-library role changes (8.3) + README/CHANGELOG/molecule, ops-control vars (impl=python, version pinned) + runbook + playbook assert; deploy to macmini, verify the binary is installed and nothing switched | S13 | — |
| S15 | Shadow | phases A–C of 9.1, comparison report in ops-control docs; fixes go back into S10–S12-style follow-up slices | S12, S14 | — |
| S16 | Cutover | 9.2–9.4 incl. rollback drill; docs/CHANGELOG final state in all three repos | S15 | — |

Acceptance criteria per slice (in addition to the green checks):

- **S1**: `cargo test` passes in CI; `pyjson` fixture cases (≥ 300 values,
  every op pair; parser cases incl. `NaN`/`Infinity`, big integers, floats
  overflowing to `inf`, duplicate keys, BOM/UTF-16/UTF-32 bytes, invalid
  documents; dumps cases for every option set used: storage, `_bounded_actual`,
  OpsGate block, httpx `json=`) all match; env fixture all match; drift test fails when a
  fixture file is edited by hand; just recipes documented.
- **S2**: every store test of 7.1 passes against the Django schema fixture;
  `--check-schema` reports missing columns/migration precisely on a
  deliberately old schema; two processes on one DB: the second exits 1
  (Rust↔Rust and Rust↔Python); contract test fails on a simulated
  migration change.
- **S3**: all decision/policy/dependency/suppression/lease fixtures
  match, covering every branch of 5.3 (the generator asserts branch coverage
  of `_should_notify_check_result`: 100 % of its lines, traced with
  `sys.monitoring` because coverage.py is not a dependency and would clash
  with an outer tracer when the drift test runs under one).
- **S4**: every replay fixture matches round by round (payload bytes via
  `pyjson`, snapshot, notifications, logs), including restore failures, store
  write failures, stale downgrade, flapping at the release boundary, summary
  batching over the character budget.
- **S5–S7**: every config fixture matches (S5 additionally the
  `metrics_eval.json` fixture and one fake-server test per redirect/decoding/
  cookie rule of 5.5); every result shape of 1.6 for the
  slice's types is produced in a fake-server test; retry counts and delays
  verified with a paused tokio clock; no test reaches the internet.
- **S8**: Telegram and OpsGate fixtures match byte-for-byte (nonce/time
  injected); token never appears in any captured log or error.
- **S9**: Rust counterparts of `tests/unit/test_shutdown_claims.py`,
  `test_notification_dampening.py`, `test_notification_reminders.py`,
  `test_notification_delivery_retry.py`, `test_notification_suppression.py`
  (handler parts), `cleaner_test.py`, `test_startup_validation.py`,
  `test_runner_shutdown_stream.py` pass; binary starts against a migrated
  copy and runs checks against fake servers.
- **S10**: counterparts of `test_collector_recovery.py`,
  `test_collector_incidents.py`, `test_collector_incident_delivery_ack.py`
  pass with an injected clock (wedged batch via a never-completing fake
  executor).
- **S11**: counterparts of `test_site_connectivity_holds.py` and the runtime
  parts of `test_site_connectivity_observer.py` pass; e2e payload sequence OK.
- **S12**: process e2e pass; `just rs-parity` green in CI with zero unmasked
  differences; docs reviewed.
- **S13**: workflow produces archive + checksum on a test tag in a fork or via
  `workflow_dispatch`; GLIBC check proves ≤ 2.35; release `agent-rs-v0.1.0`
  exists with the checksum used in S14.
- **S14**: `just test`/`lint`/`typecheck` in ops-library and ops-control;
  deploy to macmini with `impl: python`: binary at
  `/opt/nyxmon-agent/0.1.0/`, `--check-schema` OK, no drop-in, unit still runs
  Python (verified via `/proc/<pid>/exe`), no restart if nothing else changed.
- **S15**: phase A/B/C acceptance of 9.1 documented with numbers.
- **S16**: all 9.3 checks recorded, rollback drill done, soak started; final
  docs state "Rust agent is the production implementation".

---

## 11. Risks and open questions

### 11.1 Risks

- **R1 Resolver/TLS behaviour differences** (getaddrinfo order, happy
  eyeballs timing, rustls vs OpenSSL) — mitigated by the glibc target, the
  shadow run on the real host and network, and D-9.
- **R2 httpx semantics that are easy to get subtly wrong** (per-phase
  timeouts, redirect handling incl. reading redirect bodies, Authorization on
  https upgrades, decoding and the caps, `Accept-Encoding`, User-Agent, the
  batch-wide cookie jar shared by all HTTP-family checks of a batch).
  Mitigated by reimplementing redirects/decoding explicitly (5.5), fake-server
  tests per rule, the offline parity harness and the shadow comparison.
- **R3 Python quirks reproduced on purpose** might be mistaken for bugs by
  implementers and "fixed" (IMAP `password_secret`, TCP `cert_expiry` with
  status `error` but `severity: warning`, json-metrics `TypeError` on string
  timeouts → `executor_exception`, IMAP STARTTLS/login failures surfacing as
  `unexpected_error` rather than `transient_failure`).
  The fixtures pin them; changes need an owner decision and a change on both
  sides.
- **R4 Long shadow comparison noise** from endpoints that flap — the
  comparison reports both samples' timestamps; acceptance is "explained", not
  "identical".
- **R5 Site connectivity has no production baseline** (feature off). Risk
  accepted: replay fixtures generated from the real Python observer give the
  strongest available evidence; phase B exercises the live probe loop.
- **R6 SQLite lock interaction** with Django under rollback journal (agent
  `BEGIN IMMEDIATE` waits up to 5 s for dashboard readers) — same exposure as
  today; monitored via `database is locked` errors in the journal.
- **R7 Single-connection actor stall**: a hung SQLite call would stall the
  whole agent. Bounded by `busy_timeout`; a watchdog logs when one operation
  exceeds 30 s.
- **R8 Fixture generator drift on Python upgrades** (e.g. `repr`/`json`
  behaviour) — the drift test makes it visible in CI.
- **R9 Supply chain** — `Cargo.lock` committed, `--locked` everywhere,
  `cargo deny check` (licenses/advisories) in the `rust` CI job, pinned action
  SHAs, release artifacts verified by sha256 pinned in ops-control.

### 11.2 Open questions for the owner

- **Q1** Accept the glibc (not musl) target of 8.1?
- **Q2** HTTP User-Agent: keep `python-httpx/0.28.1` for zero-change parity
  (proposed for cutover), and switch to `nyxmon-agent/<ver>` later as a
  separate change?
- **Q3** IMAP ignores `password_secret` for login in Python. Keep the quirk
  (proposed) or fix in both agents as a separate change?
- **Q4** Ping checks cannot work under the unit's `NoNewPrivileges=true`
  with either agent (file capability not granted, `ping_group_range` closed).
  Out of scope for the port; should a follow-up open
  `net.ipv4.ping_group_range` for the `nyxmon` group?
- **Q5** Keep `journal_mode=delete`, or plan a separate WAL migration
  (affects Django and backups) after cutover?
- **Q6** When to retire the Python agent (and its `_ensure_schema` DDL):
  proposed after a 30-day soak, as its own change.
- **Q7** Approve the shadow run on macmini (second agent process, doubled
  probe traffic to the monitored endpoints for ~1–2 days, mail checks
  excluded, one extra SMTP test mail in phase C).
- **Q8** Approve one controlled Telegram/OpsGate test alert during cutover
  verification (9.3).
- **Q9** Toolchain pin: latest stable at S1 time, MSRV = that version.
