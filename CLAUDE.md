# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

- The architecture is documented in [Architecture.md](/docs/Architecture.md).

## Build, Lint & Test Commands
- Install dependencies: `uv sync`
- Package management: `uv pip install/uninstall <package>`
- Lint: `uvx pre-commit run --all-files` (ruff runs automatically)
- Type check: `uv mypy src/`
- Test (all): `uv run pytest`
- Test (single): `uv run pytest path/to/test.py::TestClass::test_function -v`
- Run Django server: `cd src/django && uv run manage.py runserver`
- Rust agent (`agent-rs/`, see `agent-rs/README.md`): `just rs-test`,
  `just rs-lint`, `just rs-fmt`, `just rs-deny`; `just test`/`just lint`/`just typecheck`
  include them. After changing agent behaviour in `src/nyxmon` or adding a
  Django migration, run `just rs-fixtures` (the fixture drift and schema
  contract tests fail otherwise). Never add DDL to the Python agent.
  `just rs-parity` runs the Python and the Rust agent side by side on fake
  servers and fails on any unmasked difference (CI job `parity`).

## Production Deployment
- Build and publish package: `uv build && uv publish`
- Deployment lives outside this repository: the ops-library `nyxmon_deploy`
  role, run from ops-control (`just deploy` wraps that playbook)
- Granian serves `config.wsgi_proxy:application` on loopback behind Traefik
- SQLite database, no PostgreSQL

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
- src/django holds manage.py and django settings, but no web UI business logic
- src/nyxboard contains the main Django web UI