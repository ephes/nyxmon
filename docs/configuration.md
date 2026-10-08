# Configuration

## Runtime Settings

NyxMon's agent is configured primarily through CLI flags. When running `uv run start-agent`, you can provide:

- `--db`: Path to the SQLite database file (required)
- `--interval`: Polling interval in seconds (default: 5)
- `--cleanup-interval`: Seconds between result-cleanup runs (default: 3600)
- `--retention-period`: Seconds to keep historical results (default: 86400)
- `--batch-size`: Rows deleted per cleanup batch (default: 1000)
- `--disable-cleaner`: Skip starting the results cleaner
- `--log-level`: Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)
- `--enable-telegram`: Turn on Telegram notifications (requires credentials below)

### Result Cleanup

Every `--cleanup-interval` seconds the cleaner deletes results older than
`--retention-period`. It works in batches of `--batch-size` rows, each in its
own short transaction, and keeps going until a batch comes back short, so the
whole expired backlog is removed in one cycle. It yields between batches so the
collector can keep writing. One cycle runs at most 100 batches (100,000 rows
with the default batch size); a larger backlog continues on the next cycle and
the cleaner logs a warning. Each cycle logs the total it deleted.

The retention cutoff is computed on SQLite's UTC clock, the same clock that
stamps the results, so retention is exact whatever the host's time zone.

Deleting rows does not shrink the SQLite file; SQLite reuses the freed pages
for new results. After the first cleanup of a large backlog you can reclaim
the space once with the agent stopped (back up the file first):

```bash
sqlite3 /path/to/database.sqlite 'VACUUM;'
```

### Environment Variables

Telegram notifications read:

- `TELEGRAM_BOT_TOKEN`: Bot token from BotFather
- `TELEGRAM_CHAT_ID`: Chat ID for notifications
- `NYXMON_NOTIFY_CONSECUTIVE_FAILURES`: Consecutive warning/error samples
  required before the *first* Telegram notification or OpsGate ticket of an
  incident (default `2`, range `1`–`100`). Setting `1` restores immediate
  first-failure alerting for every check; prefer the per-check override below
  when only a few checks need it.
- `NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS`: Wall-clock seconds that must elapse
  between reminders while an **error** incident stays open (default `21600`,
  six hours; range `60`–`2592000`).
- `NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS`: The same, for **warning**
  incidents (default `86400`, 24 hours; range `60`–`2592000`). Warnings are
  never silenced; they simply remind daily, because standing warnings are
  usually acknowledged conditions waiting on an operator or a third party.
- `NYXMON_NOTIFY_REPEAT_FAILURES`: **Deprecated and ignored.** Reminder cadence
  is now elapsed time rather than a sample count. A sample count cannot be
  translated into a duration, because twelve samples mean about one hour for a
  five-minute check and about twelve hours for an hourly one — exactly the
  interval dependence this setting was removed for. If the variable is still
  set, the worker logs one warning per distinct value naming the replacement
  and then ignores it. Replace it with `NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS`.
- `NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`: Per-check cooldown for results
  that explicitly ask for an immediate alert through
  `data.notification_immediate` (default `3600`). The collector no longer sets
  that flag when it reclaims an expired lease, so a stock installation does not
  use this path.
- `NYXMON_PROCESSING_LEASE_SECONDS`: Maximum time a claimed check may remain in
  `processing` before it is reclaimed, recorded as a `stale_processing_lease`
  result, and scheduled again (default `900`, minimum `30`). Invalid values
  fall back to the default and emit a warning in the worker log. Legacy claims
  without a timestamp receive a fresh full lease on first observation.
  Nyxmon automatically raises the effective lease to its conservative estimate
  of the largest enabled check's timeout/retry budget and logs when it does so.
  This is a batch-wide safety bound: one unusually slow enabled check increases
  recovery latency for every check. A check may set `data.max_runtime_seconds`
  when its valid runtime cannot be derived from the standard timeout, retry, and
  retry-delay fields. Derived estimates are capped at `3600` seconds; set the
  global lease explicitly if a deliberately longer recovery window is required.
- `NYXMON_CHECK_BATCH_SIZE`: Maximum checks claimed per collector iteration
  (default `5`, clamped to `1`–`100`). With the default
  one-second collector interval, fast checks can drain about five due checks per
  second. Slow checks and serial notification I/O reduce that throughput.
  A batch additionally receives one minute per claimed check for serial result
  persistence and notification handling. Claims are
  processed in configurable bounded batches (five by default), so the default
  allowance is five minutes and scales with `NYXMON_CHECK_BATCH_SIZE`. With the
  default lease and batch size, a wedged batch therefore stops blocking lease
  recovery within twenty minutes. New executions
  stay paused while that single abandoned thread remains alive, and the pause is
  reported as one collector incident with hourly reminders. A failed reminder
  attempt is retried after one minute. Stale-lease reclaim uses the same batch
  size but is drained in up to 20 rounds per iteration, so a restart that
  strands dozens of claims is recovered — and reported — in one go.
- `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS`: Retry a per-check alert whose send
  failed or whose outcome was ambiguous (default `0`, meaning off; otherwise
  `60`–`3600`). With `0` a failed send behaves exactly as before and is lost
  until the next reminder. When set, a notification attempt is recorded
  durably before the send and repeated on the next failing sample once the
  interval has elapsed, irrespective of the reminder window; an OK sample
  clears it. Immediate alerts (`data.notification_immediate`) participate on the
  same terms: a due retry sends again even inside the immediate cooldown, which
  bounds new alerts rather than the redelivery of one that may never have
  arrived. A notifier that never attempted a send — no Telegram credentials, no
  portal provider — reports "cannot tell" rather than a failure, so an
  installation without Telegram accumulates no intent to retry.
  Acknowledgements are written after every successful send
  regardless of this knob, so enabling it later cannot resend an alert that was
  already delivered. See {doc}`site-connectivity` for the full semantics.

Environment values that cannot be parsed, or that fall outside the documented
range, are ignored in favour of the default and warned about once per distinct
value, so a typo cannot flood the worker log.

#### Site Connectivity Observer

Nyxmon can observe its own internet connection and hold the alerts of checks
that declared they depend on it. The feature is **off** by default; nothing
changes until `NYXMON_SITE_CONNECTIVITY_MODE` is set. {doc}`site-connectivity`
documents the detection, the hold rule, the messages, and the rollout steps.

| Variable | Default | Range / notes |
| --- | --- | --- |
| `NYXMON_SITE_CONNECTIVITY_MODE` | `off` | `off`, `observe`, `enforce`. `observe` probes, persists and sends the site messages but holds nothing. |
| `NYXMON_SITE_PROBE_INTERVAL_SECONDS` | `60` | `15`–`600`. One probe round per interval, per process. |
| `NYXMON_SITE_PROBE_TIMEOUT_SECONDS` | `3` | `1`–`10`, per target. The whole round is additionally bounded at timeout plus two seconds. |
| `NYXMON_SITE_PROBE_IPV4_TARGETS` | `1.1.1.1:443,8.8.8.8:443,9.9.9.9:443` | Comma-separated `ip:port` IPv4 literals. An explicitly empty value marks the path `unobserved`. |
| `NYXMON_SITE_PROBE_IPV6_TARGETS` | `[2606:4700:4700::1111]:443,[2001:4860:4860::8888]:443,[2620:fe::fe]:443` | Comma-separated `[ip]:port` IPv6 literals; empty means `unobserved`, which is how an IPv4-only site is configured. |
| `NYXMON_SITE_PROBE_DNS_NAMES` | `cloudflare.com,google.com,quad9.net` | Comma-separated host names resolved through the system resolver; empty means `unobserved`. |
| `NYXMON_SITE_DOWN_AFTER_FAILURES` | `2` | `1`–`10` consecutive failed rounds before a path is confirmed `down`. Holding starts only at `down`. |
| `NYXMON_SITE_RECOVERY_GRACE_SECONDS` | `900` | `60`–`3600`. How long a recovered path stays `recovering` before it releases. |
| `NYXMON_SITE_MAX_HOLD_SECONDS` | `10800` | `600`–`86400`. Hard bound per check and hold; afterwards the check follows ordinary policy again. |
| `NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS` | `900` | `60`–`86400`. Age at which an ongoing outage alerts, and the threshold above which a closed outage gets a summary. |
| `NYXMON_SITE_INCIDENT_REMINDER_SECONDS` | `21600` | `60`–`2592000`. Reminder cadence of a *delivered* ongoing alert. It is also the cadence at which an outage that flapped back to `down` may attempt a new ongoing alert after an earlier, undelivered one was retired on recovery, measured from the later of the last delivery and the last attempt. |

A round fails for a path only when every one of its targets failed, so a single
dead server can never take a path down. Address literals are used on purpose for
`ipv4` and `ipv6`, so those paths do not depend on DNS.

Snapshot staleness is derived, not configured: a snapshot older than three probe
intervals is not trusted, nothing is held, and a frozen observer therefore stops
holding within about three minutes. The retry cadence of an undelivered site
message is fixed at 60 seconds.

Invalid values follow the same fail-safe rule as the reliability knobs: they are
warned about once per distinct value and replaced by the default. An empty
target list is a valid configuration, not an invalid value.

#### Suppression Freshness Guard

`notification_suppression` fetches a payload from another endpoint to decide
whether a failure is expected. When that endpoint is **the same one the check
itself monitors**, a frozen payload becomes self-silencing: if the freeze
captured a unit mid-run, the suppression source reports "still running" forever
and suppresses every later failure — including the staleness assertion that
exists to report the freeze.

Guard against it by pointing `freshness_path` at the payload's own age field:

```json
{
  "notification_suppression": {
    "url": "https://metrics.example/endpoint",
    "active_if": [
      {"path": "$.units.backup.service.active_state", "op": "==", "value": "activating"}
    ],
    "freshness_path": "$.meta.age_seconds",
    "freshness_max_seconds": 600
  }
}
```

| Key | Effect |
| --- | --- |
| `freshness_path` | JSON path to the payload's own age, in seconds. Omit to disable the guard (existing configs are unchanged). |
| `freshness_max_seconds` | Suppress nothing once the reported age exceeds this. |

The guard **fails open**: a missing field, a non-numeric value, an absent or
non-positive `freshness_max_seconds`, or an age past the limit all mean
*suppress nothing*. A stale source can therefore never silence an alert, which
is the safe direction — an unnecessary page beats a permanently hidden outage.

`active_if` rules fail open the same way: a rule matches only when its `path`
exists in the payload and holds a value of the same JSON type as the rule's
`value`. A missing path never matches, whatever the operator (so `!=` and
`== null` cannot silence an alert when the source drops or renames a field),
and neither does a type mismatch such as `true <= 24`. A suppression payload
larger than 256 KiB, a compressed response, more than five redirects, or a
payload that fails to load or parse suppresses nothing.

#### Per-Check Notification Policy

A single check can override the global thresholds through a
`notification_policy` object in its `data` JSON, alongside
`notification_suppression`. Every key is optional:

```json
{
  "notification_policy": {
    "consecutive_failures": 1,
    "reminder_seconds": 21600,
    "warning_consecutive_failures": 3,
    "warning_reminder_seconds": 86400
  }
}
```

| Key | Applies to | Range | Falls back to |
| --- | --- | --- | --- |
| `consecutive_failures` | error and warning results | `1`–`100` | `NYXMON_NOTIFY_CONSECUTIVE_FAILURES` |
| `reminder_seconds` | error and warning results | `60`–`2592000` | `NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS` (errors) / `NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS` (warnings) |
| `warning_consecutive_failures` | warning results only | `1`–`100` | `consecutive_failures`, then the global default |
| `warning_reminder_seconds` | warning results only | `60`–`2592000` | `reminder_seconds`, then the global warning default |

Resolution is per result severity. A warning result prefers the `warning_*`
key, then the unprefixed key, then the global default; an error result uses the
unprefixed key, then the global default.

Validation is fail-safe and follows the `notification_suppression` precedent:

- A non-object `notification_policy`, a non-integer value, a boolean, `null`,
  or a value outside the documented range is ignored and the global default is
  used instead. Nothing raises into the check path.
- Each rejected field is warned about once per `(check_id, field)`, so a single
  malformed check cannot flood the worker log.
- A missing key is not an error; it simply inherits the global default.
- Unknown keys inside `notification_policy` are ignored, and the object as a
  whole is ignored by the check executors, so it is safe to add to any check
  type.

Per-check policy is normally rendered by the playbook that upserts the check,
not edited by hand — the dashboard has no form field for it, and a deployment
that rewrites the whole `data` blob would drop an out-of-band edit.

#### Per-Check Site Dependency

A check declares which connectivity paths it needs through a `site_dependency`
entry in its `data` JSON, alongside `notification_policy`. It is what makes the
check eligible for having its alerts held during a site outage; without it,
nothing about the check changes.

```json
{"site_dependency": "internet"}
{"site_dependency": {"requires": ["dns", ["ipv4", "ipv6"]]}}
{"site_dependency": {"requires": ["ipv6"]}}
{"site_dependency": "none"}
```

| Form | Meaning |
| --- | --- |
| absent or `"none"` | Unclassified. Never held, never rescheduled by the recovery recheck. |
| `"internet"` | Shorthand for `{"requires": ["dns", ["ipv4", "ipv6"]]}`. The right choice for HTTP/TCP/SMTP/IMAP against a dual-stack host addressed by name, because Happy Eyeballs keeps such a check working while only one address family is broken. |
| `{"requires": [...]}` | A list of requirements. Each entry is a path name (`dns`, `ipv4`, `ipv6`) or a list of alternatives forming an any-of group. The check is affected when **any** requirement is unmet. |

A single-path requirement is unmet when that path is `down` or `recovering`. An
any-of group ignores `unobserved` members and is met as soon as one observed
member is `up` *or* `failing`, so an unconfirmed failure never holds. A group
whose members are all unobserved is met, which makes `["ipv4", "ipv6"]` behave
exactly like `ipv4` on an IPv4-only site.

Validation is fail-safe, like `notification_policy`: an unknown shorthand, a
non-object value, an empty or non-list `requires`, or an entry naming something
other than a known path is warned about once per check and treated as
unclassified. Nothing raises into the check path, and a bad edit can therefore
never suppress an alert. A single-path requirement on an *unobserved* path is
always met and can never hold; the observer logs one warning per such check at
startup so the misclassification is visible.

As with `notification_policy`, put the key in the playbook that upserts the
check. A deployment rewrites the whole `data` blob and would drop an edit made
directly in the database.

#### Notification State Storage

Failure streak, incident start time, last-notification time, and the
immediate-alert cooldown are stored in Nyxmon's internal
`check_notification_state` table. It is intentionally separate from editable
check `data` and from prunable result history, so neither dashboard edits nor
result cleanup can restart an incident.

Collector-level incidents (a wedged executor, a burst of expired leases) are
stored in the `collector_incident` table, one row per incident key. Persisting
them is what makes their deduplication and reminder cadence survive a service
restart.

When upgrading an existing installation, run `python manage.py migrate` so
migrations `0011_checknotificationstate`,
`0012_notification_reminder_timestamps` and `0013_site_connectivity_state` own
those tables. The Ansible deployment role runs Django migrations automatically.
The worker applies the same schema upgrade idempotently on start, so either
order is safe.

Migration `0013_site_connectivity_state` adds three more columns to
`check_notification_state`: `held_since` (epoch of the first sample of the
current hold, `0` when no hold budget is armed), `attempt_seq` (a monotonic
counter of external notification attempts, never reset, which fences the
acknowledgement written after a send) and `attempt_at` (epoch of the current
unacknowledged attempt, `0` when no delivery is pending). All three are
meaningful at their zero default, so no backfill is needed and the columns are
inert until the site connectivity mode or the delivery retry is switched on.

`held_since` is the *budget* marker, not a live "this check is held" flag: it
stays non-zero after the budget is exhausted and after a stale-snapshot bypass,
and a maintenance-suppressed sample carries it and `attempt_at` through
unchanged, because clearing them would re-arm the budget, drop the check from
the observer's recovery recheck and cancel an unacknowledged send. Only an OK
sample, or a failing sample evaluated while the dependency is observed recovered
on a fresh snapshot, clears it.

Migration `0012_notification_reminder_timestamps` also decides how existing
failures behave at rollout. A check that was already failing *and* had already
alerted at least once is adopted as an ongoing incident: its `last_notified_at`
is stamped with the migration time, so its next reminder is one full reminder
window away instead of being re-paged as new. A check that was failing but had
never reached the alert threshold keeps `last_notified_at = 0` and follows the
normal initial-alert threshold.

#### OpsGate Integration

OpsGate producer integration (optional) reads:

- `OPSGATE_SUBMIT_BASE_URL`: OpsGate base URL (for example `http://studio.tailde2ec.ts.net:8711`)
- `OPSGATE_SUBMIT_TOKEN`: Nyxmon submit token for OpsGate
- `OPSGATE_APPROVAL_BASE_URL`: Base URL used for approval links in notifications (defaults to submit base URL)
- `OPSGATE_TICKET_EXPIRES_SECONDS`: Ticket expiry window in seconds (default `14400`)
- `OPSGATE_SUBMIT_TIMEOUT_SECONDS`: Submit HTTP timeout in seconds (default `10`)
- `OPSGATE_SUBMIT_INCLUDE_WARNINGS`: Whether warning checks also create tickets (`false` by default)

### Django Settings

Django configuration is managed through environment variables in the `src/django/config/settings/` directory.

- `DJANGO_SECRET_KEY`: Secret key for Django (required in production)
- `DJANGO_DEBUG`: Enable debug mode (default: False in production)
- `DJANGO_ALLOWED_HOSTS`: Comma-separated list of allowed hosts

## Check Types

### HTTP Checks

The built-in HTTP executor issues a `GET` request and treats only 2xx responses as success. Redirects are followed automatically before the final response status is evaluated:

```python
{
    "type": "http",
    "url": "https://example.com/health",
    "data": {
        "timeout": 10.0,
        "retries": 3,
        "retry_delay": 10.0,
        "retry_status_codes": [502, 503, 504]
    }
}
```

`timeout` defaults to `10.0`, `retries` defaults to `0`, `retry_delay` defaults to `2.0`, and `retry_status_codes` defaults to `[502, 503, 504]`. Timeouts and request/connection errors also retry when `retries` is greater than zero. Non-transient HTTP statuses such as `404` do not retry unless explicitly listed in `retry_status_codes`.

Canonical redirects can be checked without following them by setting
`follow_redirects` to `false`, `expected_status` to the required 3xx response,
and `expected_location` to the exact absolute `Location` value. For example:

```json
{
  "follow_redirects": false,
  "expected_status": 301,
  "expected_location": "https://example.com/probe/path?query=preserved"
}
```

The check streams the response and closes it after reading the status line and
headers; the body is never downloaded, so pointing a check at a large resource
(an audio file, a feed) costs one request, not one full download per interval.
Redirects are still followed by httpx, which reads the (usually tiny) bodies
of redirect responses.

Additional response validation such as JSON assertions and response-body
matching is planned.

### TCP Checks

The TCP executor validates that a port is reachable and, optionally, that TLS negotiation works and certificates are not close to expiry:

```python
{
    "type": "tcp",
    "url": "smtp.home.wersdoerfer.de",
    "port": 587,
    "tls_mode": "starttls",           # "none", "implicit", or "starttls"
    "connect_timeout": 10,
    "tls_handshake_timeout": 10,
    "retries": 1,                     # retry transient socket or TLS failures
    "check_cert_expiry": true,        # optional certificate age check
    "min_cert_days": 14,              # warning if below this threshold
    "verify": true,                   # set false to skip certificate validation (e.g., self-signed tests)
    "starttls_protocol": "smtp",      # "smtp", "imap", "sieve" or "generic" (default)
    "starttls_command": "STARTTLS\r\n", # generic only: override the upgrade command
    "starttls_read_greeting": false   # generic only: read one greeting line first
}
```

If certificate expiry falls below `min_cert_days`, the executor returns an error result with `error_type="cert_expiry"` and `severity="warning"` in the payload.

#### STARTTLS protocols

Mail servers speak first and expect a short dialogue before they accept
STARTTLS. `starttls_protocol` selects that dialogue; every step shares the
`tls_handshake_timeout` budget:

| `starttls_protocol` | Typical ports | Dialogue before the TLS handshake |
|---------------------|---------------|-----------------------------------|
| `smtp`    | 25, 587 | read the (multi-line) `220` greeting, send `EHLO nyxmon.invalid`, expect `250`, send `STARTTLS`, expect `220` |
| `imap`    | 143     | read the `* OK` greeting, send `a1 STARTTLS`, expect `a1 OK` (untagged lines are skipped) |
| `sieve`   | 4190    | read capability lines until `OK`, send `STARTTLS`, expect `OK` |
| `generic` | custom  | optionally read one greeting line (`starttls_read_greeting`), send `starttls_command`, read one chunk and accept it if it starts with `2` or contains `ok` |

`generic` is the default, so checks created before this option keep their
behaviour. It cannot pass against SMTP, IMAP or ManageSieve servers: they send
a greeting first, which the generic probe would take as the STARTTLS reply.
The dashboard form offers "Auto-detect from port", which stores `smtp` for
ports 25/587, `imap` for 143, `sieve` for 4190 and `generic` otherwise, and
warns when a mail port uses `generic`. The form does not show the
generic-only options; editing a check keeps the stored `starttls_command` and
`starttls_read_greeting`. Existing generic checks on mail ports
need the protocol set once (edit the check and pick the protocol).

A negative or unexpected reply returns `error_type="starttls_rejected"` with
`starttls_stage` (`greeting`, `ehlo` or `starttls`) and the server's reply in
`starttls_response` (cut to 500 characters). A line over 4096 bytes, more than
64 KiB of dialogue, or (for `smtp`, `imap` and `sieve`) any data the server
sends after its STARTTLS reply and before the handshake returns
`starttls_protocol_error`. `generic` keeps its single read of the reply for
compatibility, so it cannot detect such pipelined data. A connection closed
mid-dialogue returns `starttls_connection_closed` and is retried. Successful
STARTTLS results include `starttls_protocol`, and with `check_cert_expiry`
the certificate expiry is checked after the upgrade just as for implicit TLS.

### SMTP Checks

Sends an authenticated message (typically for outbound flow checks):

```python
{
    "type": "smtp",
    "url": "smtp.home.wersdoerfer.de",   # host
    "port": 587,
    "tls": "starttls",                   # "none", "starttls", "implicit"
    "verify": true,                      # default; false skips certificate/hostname checks
    "username": "monitor@xn--wersdrfer-47a.de",
    "password_secret": "nyxmon_local_monitor_password",  # or password
    "from_addr": "monitor@xn--wersdrfer-47a.de",
    "to_addr": "wersdoerfer.mailmon@gmail.com",
    "subject_prefix": "[nyxmon-outbound]",
    "timeout": 30,
    "retries": 2,
    "retry_delay": 5
}
```

Returns `error_type` on auth failures, 4xx/5xx responses, TLS certificate failures (`tls_error`), or timeouts; includes attempts count for retry visibility. Implicit TLS and STARTTLS always verify the certificate and hostname unless `verify` is set to `false`.

### IMAP Checks

Searches a mailbox for recent messages by subject and optionally deletes them:

```python
{
    "type": "imap",
    "url": "imap.gmail.com",             # host
    "port": 993,
    "tls_mode": "implicit",             # "implicit", "starttls", "none"
    "verify": true,                      # default; false skips certificate/hostname checks
    "username": "wersdoerfer.mailmon@gmail.com",
    "password_secret": "nyxmon_gmail_app_password",  # or password
    "folder": "INBOX",
    "search_subject": "[nyxmon-outbound]",
    "max_age_minutes": 30,
    "delete_after_check": true,
    "no_recent_message_severity": "critical",  # or "warning"
    "timeout": 30,
    "retries": 2,
    "retry_delay": 10
}
```

On success returns `matched_uids` and `latest_internaldate`; empty searches are retried according to `retries`/`retry_delay` before returning `no_recent_message`, and other failures include `error_type` values such as `tls_error`, `timeout` or `execution_error`. Implicit TLS and STARTTLS always verify the certificate and hostname unless `verify` is set to `false`. `no_recent_message_severity` defaults to `critical`; set it to `warning` for third-party forwarded loopback checks that should not page on forwarding gaps.

### JSON Metrics Checks

Fetches a JSON endpoint (e.g., `/.well-known/health`) and evaluates threshold rules:

```python
{
    "type": "json-metrics",
    "url": "http://macmini.tailde2ec.ts.net:9100/.well-known/health",
    "auth": {"username": "nyxmon", "password": "secret"},  # optional basic auth
    "timeout": 10,
    "retries": 1,
    "retry_delay": 2,
    "checks": [
        {"path": "$.mail.queue_total", "op": "<", "value": 100, "severity": "warning"},
        {"path": "$.services.postfix", "op": "==", "value": "active", "severity": "critical"}
    ]
}
```

Supports operators `<`, `<=`, `>`, `>=`, `==`, `!=`; severities `warning`/`critical`; simple path resolver `$.field.subfield` or `$.items.0.value`. Failures return `error_type="threshold_failed"` with all failing rules.

A rule whose `path` does not exist in the response always fails, whatever its
operator; the failure records `"actual": null` and `"reason": "path_missing"`.
A field that is present with the JSON value `null` is compared normally. An
`actual` value whose JSON form is longer than 200 characters is stored cut
down to 200 characters with `"actual_truncated": true`.

`max_body_bytes` (default `1048576`, 1 MiB) caps how much of the response body
is read. A larger body, or a `Content-Length` above the cap, fails with
`error_type="body_too_large"` without being parsed. The cap counts decoded
bytes, checked chunk by chunk as httpx decompresses the stream, so a compressed
response is stopped once its inflated size passes the cap. Error responses
(status 400 and above) are not read. The dashboard form has no field
for the cap; a value set in the stored JSON is kept when the check is edited.

### Ping Checks

Ping checks verify that a host answers ICMP echo requests. They suit devices
without an HTTP endpoint (router, NAS, Raspberry Pis):

```python
{
    "type": "ping",
    "url": "192.168.1.1",    # IP address or host name (a URL's host is used)
    "timeout": 5,            # optional, seconds to wait per attempt (max 60)
    "count": 3,              # optional, echo requests per run (1-20)
    "interval": 1,           # optional, seconds between attempts (max 60)
    "host": "nas.local"      # optional, overrides url
}
```

Host names are resolved once per run (bounded by `timeout`); the first address
returned is pinged. Each attempt runs the system `ping` binary for a single
echo request and waits at most `timeout` seconds for the reply. The check is
`ok` when at least one attempt gets a reply, so partial loss still passes.

Result data contains `target` (the pinged address), `hostname` (when a name was
resolved), `packets_sent`, `packets_received`, `packet_loss_percent`,
`rtt_min_ms`/`rtt_max_ms`/`rtt_avg_ms`/`rtt_list_ms` on success, and an
`attempts` list with one entry per echo request. Failures use the usual
`error_type`/`error_msg` fields:

| `error_type` | Meaning |
|--------------|---------|
| `timeout` | No attempt got a reply within `timeout` |
| `unreachable` | `ping` reported an error such as "Destination Host Unreachable" |
| `dns_error` / `dns_timeout` | The host name could not be resolved |
| `permission_error` | `ping` lacks the privilege to send ICMP (see below); not retried |
| `ping_unavailable` | No `ping` binary on `PATH` or in `/sbin`, `/bin`, `/usr/bin`, `/usr/sbin` |
| `configuration_error` | Invalid `timeout`/`count`/`interval` or an empty/unsafe target |

**Requirements.** NyxMon never opens raw sockets itself, so the agent runs
unprivileged. It relies on the platform `ping` binary having ICMP privileges,
which is the default on common systems:

- **macOS / FreeBSD:** `/sbin/ping` is setuid root and gets the reply wait via
  `-W` (milliseconds). IPv6 targets use `ping6`, whose wait is enforced by
  NyxMon killing the process after `timeout` plus a two second grace. Other
  BSDs (OpenBSD, NetBSD) get no wait flag and rely on that process timeout.
- **Linux:** iputils `ping` needs either file capabilities
  (`sudo setcap cap_net_raw+ep "$(command -v ping)"`) or unprivileged ICMP via
  `sysctl net.ipv4.ping_group_range` covering the agent's group (systemd-based
  distributions usually set `0 2147483647`). Without either, results report
  `permission_error`.
- **Windows:** `ping.exe` works without elevation. Its output is localized, so
  a reply is recognized by its `TTL=` field (IPv4) or by coming from the
  pinged address (IPv6), together with the RTT on that line. This path
  is covered by unit tests only.

The NyxBoard form ("📡 Ping Check") stores the host in `url` and `timeout`,
`count` and `interval` in `data`.

### DNS Checks

DNS checks verify that resolved records include at least one expected IP and support optional resolver overrides:

```python
{
    "type": "dns",
    "url": "example.com",
    "expected_ips": ["93.184.216.34"],
    "dns_server": "8.8.8.8",    # optional
    "source_ip": "192.0.2.10",   # optional, source address to bind
    "query_type": "A",            # optional, defaults to "A"
    "timeout": 5.0                 # optional, seconds
}
```

See {doc}`dns-check-examples` for more configuration scenarios.

## Deployment Configuration

### Database Location

Keep the SQLite database outside the directory a deployment synchronises. The
recommended location is a dedicated state directory such as
`/var/lib/nyxmon/db.sqlite3`, owned by the service account, and passed to the
agent with `--db` (and to Django with `DATABASE_URL`, using the four-slash
absolute form `sqlite:////var/lib/nyxmon/db.sqlite3`).

A database that lives inside the deployed source tree shares that tree's
lifecycle, which produces two failure modes during a release:

- A source synchronisation that deletes unknown files can remove a live
  `db.sqlite3-journal`, `-wal`, or `-shm` sidecar. Excluding only the main
  database file is not enough; exclude the whole `db.sqlite3*` family.
- Rewriting the ownership of the directory that contains the database takes
  write permission on that *directory* away from the service account. In the
  default `journal_mode=delete`, SQLite must create a rollback journal next to
  the database for every write transaction, so the agent immediately fails with
  `sqlite3.OperationalError: attempt to write a readonly database` even though
  the database file itself is untouched and still writable.

Both disappear once the database lives in its own state directory. A systemd
unit with `ProtectSystem=strict` must list that directory in `ReadWritePaths=`.

### systemd (Linux)

NyxMon uses systemd for service management on Linux:

```ini
[Unit]
Description=NyxMon Monitoring Agent
After=network.target

[Service]
Type=simple
User=nyxmon
WorkingDirectory=/home/nyxmon
EnvironmentFile=/home/nyxmon/site/.env
ExecStart=/usr/local/bin/start-agent --db /var/lib/nyxmon/db.sqlite3 --enable-telegram --log-level WARNING
Restart=always

[Install]
WantedBy=multi-user.target
```

Keep Telegram credentials in a root- or service-readable environment file,
not inline in the unit. NyxMon also raises the `httpx` and `httpcore` logger
thresholds to `WARNING`: their INFO request lines include Telegram's bot token
in the API URL path and must not be retained by journald.

### launchd (macOS)

For macOS deployment, NyxMon uses launchd:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nyxmon.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/start-agent</string>
        <string>--db</string>
        <string>/var/lib/nyxmon/db.sqlite3</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

### WSGI Server

NyxMon uses granian as its WSGI server (instead of gunicorn):

```bash
granian --interface wsgi config.wsgi:application --host 0.0.0.0 --port 8000
```

## Repository Configuration

### In-Memory Store

For tests or demos you can use the in-memory store bundle:

```python
from nyxmon.adapters.repositories.in_memory import InMemoryStore

store = InMemoryStore()
```

### SQLite Store

The production-ready store persists to SQLite:

```python
from nyxmon.adapters.repositories.sqlite_repo import SqliteStore

store = SqliteStore(db_path="/path/to/database.sqlite")
```
