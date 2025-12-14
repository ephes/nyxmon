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

Only Telegram notifications currently read environment variables:

- `TELEGRAM_BOT_TOKEN`: Bot token from BotFather
- `TELEGRAM_CHAT_ID`: Chat ID for notifications

### Django Settings

Django configuration is managed through environment variables in the `src/django/config/settings/` directory.

- `DJANGO_SECRET_KEY`: Secret key for Django (required in production)
- `DJANGO_DEBUG`: Enable debug mode (default: False in production)
- `DJANGO_ALLOWED_HOSTS`: Comma-separated list of allowed hosts

## Check Types

### HTTP Checks

The built-in HTTP executor issues a `GET` request and treats any non-error status as success:

```python
{
    "type": "http",
    "url": "https://example.com/health"
}
```

Additional response validation (JSON assertions, headers, etc.) is planned.

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
    "timeout": 30,
    "retries": 2,
    "retry_delay": 10
}
```

On success returns `matched_uids` and `latest_internaldate`; failures include `error_type` such as `no_recent_message`, `timeout`, or `execution_error`.

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

### Custom Checks (SSH-JSON)

Custom checks execute a command on a remote host via SSH and evaluate threshold rules against the JSON output:

```python
{
    "type": "custom",
    "url": "root@target.host",
    "data": {
        "mode": "ssh-json",
        "target": "root@target.host",
        "command": "/usr/local/bin/metrics-script",
        "timeout": 15,
        "retries": 0,
        "retry_delay": 2,
        "ssh_args": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"],
        "checks": [
            {"path": "$.status", "op": "==", "value": "ok", "severity": "critical"},
            {"path": "$.queue_size", "op": "<", "value": 100, "severity": "warning"}
        ]
    }
}
```

Requires SSH key authentication from the Nyxmon host to the target. See {doc}`custom-ssh-json-checks` for complete documentation.

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
