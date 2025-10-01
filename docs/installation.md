# Installation

## For Users

### Using pipx (Recommended)

```bash
pipx install nyxmon
```

### Using uv

```bash
uv tool install nyxmon
```

## For Developers

### Prerequisites

- Python 3.12 or higher
- uv package manager

### Development Setup

1. Install Python (3.12 or higher):
```bash
uv python install
```

2. Create a virtual environment:
```bash
uv venv
```

3. Install dependencies and local packages in editable mode:
```bash
uv sync
```

4. Install pre-commit hooks:
```bash
uvx pre-commit install
```

5. Run tests:
```bash
uv run pytest
```

6. Run static analysis:
```bash
uv run mypy src/
```

## Database Setup

Before running the application, run the database migrations:

```bash
uv run src/django/manage.py migrate
```

This creates an SQLite database file in the project root directory.

## Next Steps

See {doc}`usage` for information on running the application.
