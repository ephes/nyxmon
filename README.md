# NyxMon

A monitoring application for services and health checks.

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

4. Install local packages in editable mode:
```shell
uv pip install -e .
```

5. Install pre-commit hooks:
```shell
uvx pre-commit install
```

2. Run tests:
```shell
uv run pytest
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
4. Start the monitoring agent with notifications enabled:
   ```shell
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