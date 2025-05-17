# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

- The architecture is documented in [Architecture.md](/docs/Architecture.md).

## Build, Lint & Test Commands
- Install dependencies: `uv sync && uv pip install -e .`
- Package management: `uv pip install/uninstall <package>`
- Lint: `uvx pre-commit run --all-files` (ruff runs automatically)
- Type check: `uvx mypy src/`
- Test (all): `uv run pytest`
- Test (single): `uv run pytest path/to/test.py::TestClass::test_function -v`
- Run Django server: `cd src/django && uv run manage.py runserver`

## Code Style Guidelines
- **Python version**: Python 3.13+
- **Package management**: Use uv for all package operations
- **Formatting**: Use black and isort through ruff via pre-commit hooks
- **Imports**: Group by stdlib, third-party, first-party; sort alphabetically
- **Naming**: snake_case for variables/functions, PascalCase for classes
- **Types**: Use type hints; support mypy strict mode
- **Error Handling**: Use explicit exception handling with appropriate error types
- **Django**: Follow Django best practices for views, models, and templates
- **Testing**: Write pytest tests for monitoring agents and Django views
- **Documentation**: Document functions with docstrings (Google style)
- **Logging**: Use Python's logging module for operational logs