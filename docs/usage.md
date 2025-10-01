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
