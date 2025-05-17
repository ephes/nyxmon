# NyxMon

A monitoring application for services and health checks.

## Setup

1. Install dependencies:
```bash
uv pip install -e ".[dev,dashboard,dashboard-dev]"
```

2. Run tests:
```bash
pytest
```

## Usage

### Starting the monitoring agent

```bash
python -m src.nyxmon.entrypoints.cli --db /path/to/database.sqlite --interval 5
```

Options:
- `--db`: Path to SQLite database file (required)
- `--interval`: Check interval in seconds (default: 5)
- `--log-level`: Set the logging level (default: INFO)
- `--enable-telegram`: Enable Telegram notifications

### Running the Django dashboard

```bash
cd src/django && python manage.py runserver
```

## Notifications

### Telegram Notifications

To enable Telegram notifications:

1. Create a Telegram bot using [BotFather](https://t.me/botfather) and get the token
2. Find your chat ID (you can use the [userinfobot](https://t.me/userinfobot))
3. Set environment variables:
   ```bash
   export TELEGRAM_BOT_TOKEN=your_bot_token
   export TELEGRAM_CHAT_ID=your_chat_id
   ```
4. Start the monitoring agent with notifications enabled:
   ```bash
   python -m src.nyxmon.entrypoints.cli --db /path/to/database.sqlite --enable-telegram
   ```

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