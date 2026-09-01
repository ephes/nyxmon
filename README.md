# NyxMon

A monitoring application for services and health checks.

## Documentation

Full documentation is available at [nyxmon.readthedocs.io](https://nyxmon.readthedocs.io/en/latest/)

## Setup for Development

1. Install Python (3.12 or higher)
```shell
uv python install
```

2. Create a virtual environment:
```shell
uv venv
```

3. Install dependencies and local packages in editable mode:
```shell
uv sync
```

4. Install pre-commit hooks:
```shell
uvx pre-commit install
```

5. Run tests:
```shell
uv run pytest
```

6. Run static analysis:
```shell
uv run mypy src/
```

## Usage

### Run Database Migrations

Before running the application, make sure to run the database migrations:

```shell
uv run src/django/manage.py migrate
```

This will create an SQLite database file in the project root directory.

### Starting the monitoring agent and Django dashboard

At the moment, there's only the development version of the monitoring agent and a
Django dashboard. You can start both of them using `honcho`:

```shell
uvx honcho start
```

This will start the monitoring agent and the Django dashboard in separate processes.
You can add services and health checks through the Django dashboard.

### Creating development data

For development and testing purposes, you can quickly generate sample services and checks using the `create_devdata` management command:

```shell
uv run src/django/manage.py create_devdata
```

This command creates:
- A "Development Server" service
- A "Dashboard Check" that monitors http://localhost:8000/ (should pass when the server is running)
- A "Failing Check" that monitors a non-existent URL http://localhost:8000/non-existent-url/ (will fail)

If you already have data and want to add the development data anyway, use the `--force` flag:

```shell
uv run src/django/manage.py create_devdata --force
```

The checks run every 60 seconds, so after starting the monitoring agent, you'll see results within a minute.

## Build the package
The build backend has to be hatchling to allow for multiple top level packages (nyxmon and nyxboard). To build the
package, run:

```shell
uv build --sdist --wheel
```

And then publish the package:
```shell
uv publish --token pypi-your-token
```

### The start-agent command

The monitoring agent registers an entrypoint named `start-agent` in the
`pyproject.toml` file.

```shell
uv run start-agent --db /path/to/database.sqlite
```

Options:
- `--db`: Path to SQLite database file (required)
- `--interval`: Check interval in seconds (default: 5)
- `--log-level`: Set the logging level (default: INFO)
- `--enable-telegram`: Enable Telegram notifications

### Running the Django dashboard

```shell
PYTHONUNBUFFERED=true uv run src/django/manage.py runserver 0.0.0.0:8000
```

## Notifications

### Telegram Notifications

To enable Telegram notifications:

1. Create a Telegram bot using [BotFather](https://t.me/botfather) and get the token
2. Find your chat ID (you can use the [userinfobot](https://t.me/userinfobot))
3. Set environment variables:
   ```shell
   export TELEGRAM_BOT_TOKEN=your_bot_token
   export TELEGRAM_CHAT_ID=your_chat_id
   ```
   Or set them in your `.env` file. They'll get loaded automatically by `honcho`.

### OpsGate Ticket Producer (Phase 4A)

Nyxmon can create OpsGate tickets directly on check failures and include an approval
link in Telegram notifications.

Set these optional environment variables on the monitor process:

```shell
export OPSGATE_SUBMIT_BASE_URL=http://studio.tailde2ec.ts.net:8711
export OPSGATE_SUBMIT_TOKEN=<opsgate_submit_token_nyxmon>
export OPSGATE_APPROVAL_BASE_URL=http://studio.tailde2ec.ts.net:8711
```

Optional tuning:

```shell
export NYXMON_NOTIFY_CONSECUTIVE_FAILURES=2
export NYXMON_NOTIFY_REPEAT_FAILURES=12
export NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS=3600
export NYXMON_PROCESSING_LEASE_SECONDS=900
export NYXMON_CHECK_BATCH_SIZE=5
export OPSGATE_TICKET_EXPIRES_SECONDS=14400
export OPSGATE_SUBMIT_TIMEOUT_SECONDS=10
export OPSGATE_SUBMIT_INCLUDE_WARNINGS=false
```

`NYXMON_NOTIFY_CONSECUTIVE_FAILURES` applies to Telegram and OpsGate. The first
failing sample is still stored in history; notifications are sent when the
configured warning/error streak is reached. Persistent failures send another
notification after every `NYXMON_NOTIFY_REPEAT_FAILURES` additional samples.
Lease-expiry notifications bypass that failure threshold but are limited to one
per continuously failing check per `NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`.
A successful sample resets the cooldown so a later incident alerts immediately.

The collector treats `processing` as a lease rather than a permanent state. A
check still processing after `NYXMON_PROCESSING_LEASE_SECONDS` is released,
records an immediate `stale_processing_lease` error, and is scheduled again.
Legacy processing rows without a claim timestamp receive one full lease from
the time the collector first sees them before recovery, avoiding duplicate work.
Recovery cannot cancel an executor that is still hung outside Nyxmon, so every
executor must have a finite timeout and the lease must exceed its longest valid
runtime. Nyxmon estimates that runtime from each check's timeout/retry settings
and uses the largest enabled-check estimate as the batch-wide lease, automatically
extending an undersized configured value and logging a warning;
`data.max_runtime_seconds` can provide an explicit budget for custom behavior.
Derived per-check estimates are capped at one hour so a mistyped retry value
cannot disable recovery; operators can still set a deliberately larger global
`NYXMON_PROCESSING_LEASE_SECONDS`.
The collector abandons its wait after that execution budget plus a conservative
result persistence/notification allowance of one minute per claimed check, so lease
recovery and alerting resume even if the underlying executor thread remains
wedged. New executions stay paused until that thread exits. With the default
lease, the worst-case batch wait is twenty minutes. Keep the configured lease
at least five minutes plus one minute per claimed check so a service restart
during valid result handling cannot reclaim the batch early.
Nyxmon permits only one abandoned batch at a time; lease recovery and alerts
continue, but another execution batch is not launched until that thread exits.
While execution remains paused, Nyxmon retries an hourly collector-wide reminder
through an isolated unit of work. That reminder is not suppressed by the
per-check immediate-alert cooldown or check-level maintenance suppression,
including when the check was disabled after the abandoned batch started. A
failed reminder attempt is retried after one minute instead of on every
collector iteration.
The reminder uses the distinct `collector_execution_paused` error type and does
not reschedule its anchor check.
After recording expiry, Nyxmon waits the check's normal interval before the
replacement run. A late result from the expired claim is stored but cannot
release or reschedule a newer active claim, change its notification streak, or
trigger an alert.
Unexpected executor exceptions are likewise converted into ordinary error
results so a single broken check cannot strand its batch; exception details stay
in the worker log rather than the stored result.

Checks can also set `data.notification_suppression` to suppress Telegram and
OpsGate side effects while a maintenance endpoint reports an active or recently
finished window. Nyxmon still stores the warning/error result with
`notification_suppressed` metadata, and suppressed failures do not count toward
the consecutive failure threshold.

### Creating a Custom Notifier

You can create a custom notifier by implementing the `Notifier` interface:

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

The choice of sqlite as a database backend was deliberate. How to monitor a database going down, when you depend
on the database to monitor?
