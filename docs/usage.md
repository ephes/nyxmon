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

#### When a Check Alerts

By default, Nyxmon persists the first warning/error sample but waits for 2
consecutive non-OK samples before sending a Telegram notification or creating an
OpsGate ticket. That is the initial-alert threshold, configured globally with
`NYXMON_NOTIFY_CONSECUTIVE_FAILURES` or per check (see below).

Once an incident has alerted, further failing samples do not alert again until a
reminder is due. Reminders are timed on **elapsed wall-clock time**, not on a
number of samples: an open error incident reminds every
`NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS` (six hours by default) and an open
warning incident every `NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS`
(24 hours by default). A five-minute check and an hourly check therefore remind
on the same schedule. A reminder is evaluated when a failing sample arrives, so
it goes out on the first failing sample after the window has elapsed; a check
whose interval is longer than its reminder window reminds at its own interval.
The older sample-count setting
`NYXMON_NOTIFY_REPEAT_FAILURES` is deprecated and ignored; if it is still set,
the worker logs one warning naming its replacement.

An OK sample closes the incident: the streak, the reminder clock, and the
immediate-alert cooldown all reset, so a later failure is a genuinely new
incident and alerts on its own merits. A sample written inside a maintenance
suppression window is stored but also breaks the incident, and does not count
toward the threshold.

#### Alerting Policy for a Single Check

A check can override both knobs through a `notification_policy` object in its
`data`. To page on the very first failing sample for one critical check:

```json
{
  "notification_policy": {
    "consecutive_failures": 1
  }
}
```

The full schema, the warning-specific keys, the accepted ranges, and the
fail-safe validation behaviour are documented in
{doc}`configuration`. Anything malformed is warned about once and falls back to
the global default, so a bad edit can never silently *raise* a threshold or
disable alerting.

Because a deployment that upserts a check rewrites its whole `data` blob, set
the policy in the playbook or script that owns the check rather than editing the
database directly.

#### Per-Check Alerts Versus Collector Incidents

Nyxmon sends two kinds of alert.

A **per-check alert** names one check, carries that check's URL and error, and
is the ordinary path described above. Its OpsGate ticket key is
`nyxmon-check-<check_id>`.

A **collector incident** describes the monitoring worker itself, not any single
endpoint. It is sent once for the whole event, deduplicated across collector
iterations *and across process restarts*, and reminded about on elapsed time.
The synthetic check row it uses is never stored; its OpsGate ticket key is
`nyxmon-collector-<incident_key>`. There are two:

| Incident key | Error type | Opened when | Reminder | Resolved when |
| --- | --- | --- | --- | --- |
| `collector:stale_processing_lease` | `stale_processing_lease_batch` | an iteration reclaims at least one expired processing lease | hourly | no lease has been reclaimed for 15 minutes |
| `collector:execution_paused` | `collector_execution_paused` | a batch exceeds its execution/result deadline and new executions are paused | hourly | the wedged executor thread exits, or the worker restarts |

The stale-lease message reports how many leases were reclaimed, over what
window, across how many collector iterations, and a bounded sample of the
affected check ids (at most ten in the message, twenty retained). The
execution-paused message reports how long execution has been paused and how many
checks the wedged batch holds.

Because the state lives in the `collector_incident` table, a deploy in the
middle of an incident does not restart its alert cadence. The one deliberate
exception is `collector:execution_paused`: a wedged thread cannot outlive its
process, so the worker closes that incident on startup and logs it; a genuinely
new wedge afterwards alerts again.

#### Processing Leases and Stale Recovery

Checks are claimed with a fifteen-minute processing lease. If a worker or executor
disappears before storing a result, Nyxmon reclaims the check, records a
`stale_processing_lease` result, and schedules it again. Configure the lease with
`NYXMON_PROCESSING_LEASE_SECONDS`; keep it above the longest legitimate check
runtime. A legacy processing row without a timestamp first receives a complete
lease, preventing an immediate duplicate run while still guaranteeing recovery.

The reclaimed result is stored so a check's history shows why it has a gap, but
it never produces a per-check notification. It is marked as collector-internal:
it neither advances nor resets that check's own incident, and it does not alert
even when the check carries `consecutive_failures: 1`. Reclaiming 36 checks
after a restart therefore produces 36 result rows and exactly one
`collector:stale_processing_lease` alert. Reclaim is drained within a single
collector iteration (up to 20 bounded rounds), so that one alert reports the
real size of the event rather than one batch-sized slice of it.

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
allowed at a time: stale leases continue to be released, while new
executions wait for that thread to exit instead of consuming the worker pool.
The paused state is reported as the `collector:execution_paused` incident, is
independent of any single check's cooldown or maintenance suppression, and stays
visible even if every member of the abandoned batch is disabled. A failed
send is retried after one minute instead of waiting out the full hour.
The recovered check waits one normal check interval after its lease expiry
before running again, avoiding an immediate retry pile-up. Results arriving from
an expired claim remain in history but cannot clear a newer run's active lease.
They also cannot change that run's notification streak or trigger an alert.

Claim batches default to five checks and can be tuned with
`NYXMON_CHECK_BATCH_SIZE` (range 1–100). The collector runs once per second by
default, so fast checks can drain roughly five due checks per second; slow or
failing checks reduce that throughput. Keep the value low enough that its
worst-case serial notifier time fits the intended abandon deadline.

Notification streak, incident start time, and last-notification time are stored
per check in an internal table rather than derived from retained result history,
so result cleanup cannot cause an alert storm. The state does not live in the
editable check JSON. Results that explicitly request an immediate alert through
`data.notification_immediate` still bypass the streak threshold and are limited
to one per continuously failing check per hour by default, tunable with
`NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`; the collector no longer sets that
flag, so a stock installation does not use this path.

#### Maintenance Suppression

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

## Notification Troubleshooting

### "I deployed and got no alerts for existing failures"

That is intended. When the elapsed-time reminder columns are first added — by
migration `0012_notification_reminder_timestamps`, or by the worker's own
idempotent schema upgrade, whichever runs first — existing failure streaks are
*adopted* rather than treated as new incidents:

- A check that was failing and had already alerted at least once gets its
  `last_notified_at` stamped with the upgrade time. Its next reminder is one
  full reminder window away. It is not re-paged.
- A check that was failing but had never reached the alert threshold keeps
  `last_notified_at = 0` and still gets its legitimate first alert once the
  threshold is met.
- Healthy checks are untouched.

Without this, a rollout would page for every already-known failure at once. If
you want the current state instead of waiting for a reminder, read the dashboard
or query the results table; silence here means "already reported", not "not
failing". The worker logs the upgrade, including the epoch the streaks were
adopted at.

The other reason for quiet after a deploy is the safer default threshold: the
global default is now 2 consecutive failing samples, so a single transient blip
no longer pages. Set `notification_policy.consecutive_failures` to `1` on the
few checks that genuinely must page on the first sample.

### "The executor is wedged"

Symptom: `check batch exceeded its <n>s execution/result deadline` in the worker
log, followed by an alert with error type `collector_execution_paused`, and no
new check executions.

What Nyxmon does on its own: it stops launching new batches (only one abandoned
batch is allowed at a time), keeps reclaiming and rescheduling expired
processing leases, opens the `collector:execution_paused` incident, alerts once,
and reminds hourly for as long as the wedged thread lives. It cannot kill a
thread that is blocked outside its control.

What to check:

1. Which checks the wedged batch holds — the alert message lists a bounded
   sample of the ids.
2. Whether those checks have a finite timeout. A check whose executor can block
   forever is the usual cause; `timeout`, `connect_timeout`, `retries`, and
   `retry_delay` in the check's `data` bound it.
3. Whether `NYXMON_PROCESSING_LEASE_SECONDS` is above the longest legitimate
   runtime, and whether `data.max_runtime_seconds` is set for checks whose valid
   runtime cannot be derived from those fields.

Restarting the worker clears the wedge, because the thread cannot outlive its
process. The incident is closed on the next startup and logged; a genuinely new
wedge afterwards alerts again. Repeated `collector:execution_paused` incidents
mean a check needs a timeout, not a longer lease.

### "One restart produced dozens of stale-lease results"

Expected, and it is one alert. Every check that was `processing` when the worker
stopped has its lease reclaimed on the next iteration; each one gets a
`stale_processing_lease` result in its history so the gap is explainable, and
the whole sweep is reported once as the `collector:stale_processing_lease`
incident. Those results never page per check, so a routine deploy costs one
notification rather than one per in-flight check. The incident resolves once no
lease has been reclaimed for fifteen minutes.

If the incident keeps reminding hourly, leases are still expiring: look for a
check that runs longer than the lease, or a worker that keeps dying.

### "The worker cannot write to the database"

Symptom: `sqlite3.OperationalError: attempt to write a readonly database`,
usually one iteration during a deployment, with the database file itself
unchanged and still owned by the service account.

In the default `journal_mode=delete`, SQLite creates a rollback journal next to
the database for every write transaction, so the service account needs write
permission on the *containing directory*, not just on the file. A deployment
that rewrites that directory's ownership — or that deletes a live `-journal`,
`-wal`, or `-shm` sidecar — breaks writes for as long as the window lasts.

Keep the database in its own state directory (for example `/var/lib/nyxmon/`),
outside anything a release synchronises, and give the systemd unit
`ReadWritePaths=` for it. See {doc}`configuration` for the details. Verify the
live setup with:

```bash
sqlite3 -readonly /var/lib/nyxmon/db.sqlite3 'PRAGMA journal_mode;'
systemctl show nyxmon-monitor -p ExecStart
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
