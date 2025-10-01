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

### JSON-HTTP Checks *(planned)*

> **Not yet implemented:** A JSON-aware executor will validate JSON payloads (status codes, body fields) separately from plain HTTP checks.

### Ping Checks *(planned)*

> **Not yet implemented:** Future work will add ICMP reachability checks.

### Metric Checks *(planned)*

> **Not yet implemented:** Planned support for asserting numeric metrics fall within configured bounds.

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
