# Usage

## Starting the Application

### Development Mode

Start both the monitoring agent and Django dashboard using honcho:

```bash
uvx honcho start
```

This will start:
- The monitoring agent (executing checks)
- The Django dashboard at http://localhost:8000

### Production Mode

For production deployment, see the deployment section below.

## Managing Checks

### Creating Development Data

For development and testing, generate sample services and checks:

```bash
uv run src/django/manage.py create_devdata
```

This creates:
- A "Development Server" service
- A "Dashboard Check" monitoring http://localhost:8000/ (passes when server is running)
- A "Failing Check" monitoring http://localhost:8000/non-existent-url/ (will fail)

To force adding data even if checks already exist:

```bash
uv run src/django/manage.py create_devdata --force
```

### Creating Checks via the Dashboard

The NyxBoard web UI provides an intuitive way to create and manage health checks without writing JSON or using the CLI.

#### Check Type Selection

When creating a new check, click any "Add Check" or "Create Health Check" button to reveal a dropdown menu with available check types:

- **🌐 HTTP Check** - Monitor web endpoint availability
- **📋 JSON HTTP Check** - Monitor JSON API endpoints
- **🔍 DNS Check** - Monitor DNS resolution and validate IPs

Select the check type to open the appropriate form.

#### Creating DNS Checks

DNS check forms are organized into logical sections:

**Basic Information:**
- **Name**: Descriptive name for the check (e.g., "DNS - home.wersdoerfer.de from LAN")
- **Service**: Associate the check with a service
- **Check Interval**: How often to run the check (in seconds)
- **Disabled**: Toggle to temporarily disable the check

**DNS Configuration:**
- **Domain**: The domain to query (e.g., `home.wersdoerfer.de` or `example.com`)
- **Expected IPs**: One IP per line - check succeeds if DNS returns any of these IPs
- **Query Type**: A (IPv4) or AAAA (IPv6) record type

**Advanced Options** (optional):
- **DNS Server**: Specific DNS server to query (uses system default if empty)
- **Source IP**: Source IP to bind for the query (for split-horizon DNS validation)
- **Timeout**: Query timeout in seconds (default: 5.0)

#### Form-to-JSON Mapping

The dashboard form fields map directly to the DNS check JSON structure:

| Form Field | JSON Field | Example |
|------------|-----------|---------|
| Domain | `url` | `"example.com"` |
| Expected IPs (one per line) | `expected_ips` | `["192.168.1.1", "192.168.1.2"]` |
| Query Type | `data.query_type` | `"A"` or `"AAAA"` |
| DNS Server | `data.dns_server` | `"192.168.178.94"` |
| Source IP | `data.source_ip` | `"192.168.178.50"` |
| Timeout | `data.timeout` | `5.0` |

**Example:** Creating a split-horizon DNS check via the form:
```
Name: DNS - home from LAN
Domain: home.wersdoerfer.de
Expected IPs:
  192.168.178.94
Query Type: A
DNS Server: 192.168.178.94
Source IP: 192.168.178.50
```

Results in this JSON:
```json
{
  "url": "home.wersdoerfer.de",
  "data": {
    "expected_ips": ["192.168.178.94"],
    "query_type": "A",
    "dns_server": "192.168.178.94",
    "source_ip": "192.168.178.50"
  }
}
```

**Tip:** For detailed DNS check examples and troubleshooting, see {doc}`dns-check-examples`.

### Using the CLI

The monitoring agent can be run directly with custom options:

```bash
uv run start-agent --db /path/to/database.sqlite
```

**Options:**
- `--db`: Path to SQLite database file (required)
- `--interval`: Check polling interval in seconds (default: 5)
- `--cleanup-interval`: Seconds between result-cleanup runs (default: 3600)
- `--retention-period`: Seconds to retain historical results (default: 86400)
- `--batch-size`: Maximum results deleted per cleanup run (default: 1000)
- `--disable-cleaner`: Skip scheduling the results cleaner
- `--log-level`: Set the logging level (default: INFO)
- `--enable-telegram`: Enable Telegram notifications

### Running the Django Dashboard

```bash
PYTHONUNBUFFERED=true uv run src/django/manage.py runserver 0.0.0.0:8000
```

## Notifications

### Telegram Notifications

To enable Telegram notifications:

1. Create a Telegram bot using [BotFather](https://t.me/botfather) and get the token
2. Find your chat ID using [userinfobot](https://t.me/userinfobot)
3. Set environment variables:
   ```bash
   export TELEGRAM_BOT_TOKEN=your_bot_token
   export TELEGRAM_CHAT_ID=your_chat_id
   ```
   Or set them in your `.env` file for automatic loading with honcho.

By default, Nyxmon persists the first warning/error sample but waits for 2
consecutive non-OK samples before sending Telegram notifications or creating
OpsGate tickets. Set `NYXMON_NOTIFY_CONSECUTIVE_FAILURES=1` to restore immediate
first-failure alerts. While a failure persists, Nyxmon sends a reminder after
every 12 additional failing samples; tune that with
`NYXMON_NOTIFY_REPEAT_FAILURES`.

Checks are claimed with a fifteen-minute processing lease. If a worker or executor
disappears before storing a result, Nyxmon reclaims the check, emits an immediate
`stale_processing_lease` error, and schedules it again. Configure the lease with
`NYXMON_PROCESSING_LEASE_SECONDS`; keep it above the longest legitimate check
runtime. A legacy processing row without a timestamp first receives a complete
lease, preventing an immediate duplicate run while still guaranteeing recovery.
Nyxmon cannot forcibly cancel an executor that remains hung outside its worker;
use finite executor timeouts and keep the lease above the longest valid runtime.
Nyxmon derives a conservative minimum from enabled checks' timeout and retry
configuration and extends an undersized lease with a warning. The largest
enabled-check estimate is used for the whole batch, so an unusually slow check
also lengthens recovery detection for faster checks. For custom check behavior,
set `data.max_runtime_seconds` to its maximum legitimate runtime. Derived
per-check values are capped at one hour; a deliberately longer recovery
window must be set globally with `NYXMON_PROCESSING_LEASE_SECONDS`. At the lease
budget plus a result persistence/notification allowance of one minute per
claimed check, the collector abandons waiting for the executor thread and resumes
lease recovery and alerting; with the default lease this is at most twenty minutes.
New executions remain paused while the wedged thread continues in the background.
Only one abandoned batch is
allowed at a time: stale leases continue to be released and alerted, while new
executions wait for that thread to exit instead of consuming the worker pool.
An hourly paused-collector reminder uses an isolated unit of work and bypasses
the per-check immediate-alert cooldown and check-level maintenance suppression;
disabling every member of the abandoned batch does not silence that process-level
warning. Failed reminder attempts use a one-minute retry backoff.
The reminder is recorded as `collector_execution_paused`, distinct from a
single check's lease expiry, and does not alter the anchor check's schedule or
ordinary immediate-alert cooldown.
The recovered check waits one normal check interval after the lease alert before
running again, avoiding an immediate retry pile-up. Results arriving from an
expired claim remain in history but cannot clear a newer run's active lease.
They also cannot change that run's notification streak or trigger an alert.

Claim and stale-recovery batches default to five checks and can be tuned with
`NYXMON_CHECK_BATCH_SIZE` (range 1–100). The collector runs once per second by
default, so fast checks can drain roughly five due checks per second; slow or
failing checks reduce that throughput. Keep the value low enough that its
worst-case serial notifier time fits the intended abandon deadline.

Notification streak and reminder counters are stored per check in an internal
table rather than derived from retained result history, so result cleanup cannot
cause an alert storm. The state does not live in the editable check JSON.
Immediate lease-expiry alerts do not advance the ordinary reminder counter and
are limited to one per continuously failing check per hour by default; a
successful sample resets that cooldown for the next incident. Tune it with
`NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`.

Checks may suppress Telegram and OpsGate side effects during known maintenance by
adding `data.notification_suppression`. The check still runs and the result is
still stored; warning/error samples written during the window include
`notification_suppressed` metadata and do not count toward the consecutive
failure threshold.

Example:

```json
{
  "notification_suppression": {
    "url": "http://host.example:9106/.well-known/os-apt-maintenance",
    "timeout": 3.0,
    "reason": "host_os_apt_maintenance",
    "status_path": "$.last_status",
    "active_statuses": ["running"],
    "finished_epoch_path": "$.last_run_finished_epoch",
    "active_for_seconds": 900,
    "auth": {
      "username": "nyxmon",
      "password": "secret"
    }
  }
}
```

### OpsGate Approval Workflow (Optional)

To have Nyxmon create OpsGate remediation tickets directly and append approval links to
Telegram alerts, set:

```shell
export OPSGATE_SUBMIT_BASE_URL=http://studio.tailde2ec.ts.net:8711
export OPSGATE_SUBMIT_TOKEN=<opsgate_submit_token_nyxmon>
export OPSGATE_APPROVAL_BASE_URL=http://studio.tailde2ec.ts.net:8711
```

### Creating Custom Notifiers

Implement the `Notifier` interface for custom notifications:

```python
from nyxmon.adapters.notification import Notifier

class CustomNotifier(Notifier):
    def notify_check_failed(self, check, result):
        # Implement your notification logic
        pass

    def notify_service_status_changed(self, service, status):
        # Implement your notification logic
        pass

# In your setup code (modify bootstrap.py or cli.py):
my_notifier = CustomNotifier()
bus = bootstrap(notifier=my_notifier)
```

## Deployment

NyxMon uses SQLite as its database backend by design - this avoids dependency on a database for monitoring the database itself.

### Building the Package

```bash
uv build --sdist --wheel
```

### Publishing the Package

```bash
uv publish --token pypi-your-token
```

### Deployment Commands

NyxMon includes deployment commands for different environments:

- `deploy-staging`: Deploy to staging environment
- `deploy-production`: Deploy to production environment
- `deploy-macos`: Deploy to macOS (uses launchd instead of systemd)
- `remove-macos`: Remove macOS deployment

See {doc}`configuration` for deployment configuration details.
