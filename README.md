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