# NyxMon Documentation

Welcome to the NyxMon documentation. NyxMon is a minimalistic monitoring application for services and health checks, designed with a clean architecture approach and SQLite as its storage backend.

## Quick Start

1. Install NyxMon with `pipx install nyxmon` or `uv tool install nyxmon`
2. Run database migrations: `uv run src/django/manage.py migrate`
3. Start the agent and dashboard: `uvx honcho start`
4. Access the dashboard at http://localhost:8000

```{note}
For detailed installation instructions and development setup, see {doc}`installation`.
```

## Key Features

- **Event-Driven Architecture**: Built on a message bus pattern with commands and events
- **Multiple Check Types**: HTTP checks and DNS checks today (ping and metric validation are planned)
- **Django Dashboard**: Web interface for monitoring and configuration
- **Telegram Notifications**: Optional notifications for check failures
- **SQLite Backend**: Deliberate choice to avoid database dependencies for monitoring
- **Async Check Runner**: Efficient concurrent check execution

## Contents

```{toctree}
:maxdepth: 2
:caption: Guides

installation
usage
configuration
dns-check-examples
smtp-checks
imap-checks
json-metrics-check-examples
```

```{toctree}
:maxdepth: 2
:caption: Architecture

Architecture
async-check-runner
concurrency-overview
```

```{toctree}
:maxdepth: 2
:caption: Reference

api
```

## Additional Resources

- Source code: <https://github.com/ephes/nyxmon>
- {doc}`Architecture Overview <Architecture>` - detailed system design and component documentation
- {doc}`Async Check Runner <async-check-runner>` - detailed walkthrough of the asynchronous runner implementation
