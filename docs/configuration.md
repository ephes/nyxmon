# Configuration

## Runtime Settings

NyxMon's agent is configured primarily through CLI flags. When running `uv run start-agent`, you can provide:

- `--db`: Path to the SQLite database file (required)
- `--interval`: Polling interval in seconds (default: 5)
- `--cleanup-interval`: Seconds between result-cleanup runs (default: 3600)
- `--retention-period`: Seconds to keep historical results (default: 86400)
- `--batch-size`: Maximum results deleted per cleanup batch (default: 1000)
- `--disable-cleaner`: Skip starting the results cleaner
- `--log-level`: Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)
- `--enable-telegram`: Turn on Telegram notifications (requires credentials below)

### Environment Variables

Telegram notifications read:

- `TELEGRAM_BOT_TOKEN`: Bot token from BotFather
- `TELEGRAM_CHAT_ID`: Chat ID for notifications
- `NYXMON_NOTIFY_CONSECUTIVE_FAILURES`: Consecutive warning/error samples required before sending Telegram notifications or creating OpsGate tickets (default `2`; set `1` for immediate first-failure alerts)
- `NYXMON_NOTIFY_REPEAT_FAILURES`: Additional consecutive warning/error samples
  between persistent-failure reminders (default `12`)
- `NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`: Per-check cooldown for immediate
  processing-lease expiry alerts (default `3600`)
- `NYXMON_PROCESSING_LEASE_SECONDS`: Maximum time a claimed check may remain in
  `processing` before it is reclaimed and emits an immediate
  `stale_processing_lease` error (default `900`, minimum `30`). Invalid values
  fall back to the default and emit a warning in the worker log. Legacy claims
  without a timestamp receive a fresh full lease on first observation.
  Nyxmon automatically raises the effective lease to its conservative estimate
  of the largest enabled check's timeout/retry budget and logs when it does so.
  This is a batch-wide safety bound: one unusually slow enabled check increases
  recovery latency for every check. A check may set `data.max_runtime_seconds`
  when its valid runtime cannot be derived from the standard timeout, retry, and
  retry-delay fields. Derived estimates are capped at `3600` seconds; set the
  global lease explicitly if a deliberately longer recovery window is required.
- `NYXMON_CHECK_BATCH_SIZE`: Maximum checks claimed or stale leases reclaimed
  per collector iteration (default `5`, clamped to `1`–`100`). With the default
  one-second collector interval, fast checks can drain about five due checks per
  second. Slow checks and serial notification I/O reduce that throughput.
  A batch additionally receives one minute per claimed check for serial result
  persistence and notification handling. Claims and stale recoveries are
  processed in configurable bounded batches (five by default), so the default
  allowance is five minutes and scales with `NYXMON_CHECK_BATCH_SIZE`. With the
  default lease and batch size, a wedged batch therefore stops blocking lease
  recovery within twenty minutes. New executions
  stay paused while that single abandoned thread remains alive, and an hourly
  paused-collector reminder bypasses the ordinary per-check immediate cooldown
  and maintenance suppression. A failed reminder attempt is retried after one
  minute.

Failure streak, reminder, and immediate-alert cooldown state is stored in
Nyxmon's internal `check_notification_state` table. It is intentionally separate
from editable check `data` and from prunable result history.

When upgrading an existing installation, run `python manage.py migrate` so
migration `0011_checknotificationstate` owns that table. The Ansible deployment
role runs Django migrations automatically.

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
    "starttls_command": "STARTTLS\r\n"  # override if a different upgrade command is required
}
```

If certificate expiry falls below `min_cert_days`, the executor returns an error result with `error_type="cert_expiry"` and `severity="warning"` in the payload.

### SMTP Checks

Sends an authenticated message (typically for outbound flow checks):

```python
{
    "type": "smtp",
    "url": "smtp.home.wersdoerfer.de",   # host
    "port": 587,
    "tls": "starttls",                   # "none", "starttls", "implicit"
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

Returns `error_type` on auth failures, 4xx/5xx responses, or timeouts; includes attempts count for retry visibility.

### IMAP Checks

Searches a mailbox for recent messages by subject and optionally deletes them:

```python
{
    "type": "imap",
    "url": "imap.gmail.com",             # host
    "port": 993,
    "tls_mode": "implicit",             # "implicit", "starttls", "none"
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

On success returns `matched_uids` and `latest_internaldate`; empty searches are retried according to `retries`/`retry_delay` before returning `no_recent_message`, and other failures include `error_type` values such as `timeout` or `execution_error`. `no_recent_message_severity` defaults to `critical`; set it to `warning` for third-party forwarded loopback checks that should not page on forwarding gaps.

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

### Ping Checks *(planned)*

> **Not yet implemented:** Future work will add ICMP reachability checks.

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
ExecStart=/usr/local/bin/start-agent --db /var/lib/nyxmon/db.sqlite
Restart=always

[Install]
WantedBy=multi-user.target
```

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
        <string>/var/lib/nyxmon/db.sqlite</string>
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
