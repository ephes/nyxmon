# Repository Guidelines

## Project Structure & Module Organization
NyxMon separates the monitoring engine from its Django UI. Agent code, adapters, and CLI entrypoints live in `src/nyxmon`. The main Django app lives in `src/nyxboard` (templates, views, static assets for NyxBoard). `src/django` only hosts the project wrapper—`manage.py`, Django settings modules, and ASGI/WSGI bootstrap—so treat it as config glue. Tests mirror this split: unit specs in `tests/unit`, dashboard coverage in `tests/dashboard`, and integration flows in `tests/e2e`. Keep new fixtures near their consumers and avoid committing generated artifacts from Django collects.

## Build, Test, and Development Commands
Use the `justfile` as the main entry point (`just --list`). `just test` runs the Python suite, `just lint` executes the Ruff pre-commit hooks, and `just typecheck` wraps `uv run mypy src/`. Start the Django dev server with `just dev-server`, or run management tasks via `just manage <command>`. Under the hood we rely on `uv`: `uv sync` installs dependencies and `uv run` executes scripts. UI-specific checks (if you touch frontend JS) rely on `npm test` from the project root. The Rust port of the agent lives in `agent-rs/` (plan: `agent-rs/PLAN.md`, usage: `agent-rs/README.md`); `just test`, `just lint` and `just typecheck` also run its checks, `just rs-test`/`just rs-lint`/`just rs-fmt` run them alone, and `just rs-fixtures` regenerates its Python-generated fixtures (required whenever agent behaviour in `src/nyxmon` changes or a Django migration is added; schema changes are Django migrations, never DDL in the Python agent).

## Coding Style & Naming Conventions
Formatting flows through `pre-commit` using Ruff (`uvx pre-commit run --all-files`). Python modules use 4‑space indentation and double quotes by default; prefer snake_case for files/functions and PascalCase for classes. Mirror existing naming when adding Django models, forms, or HTMX views inside `src/nyxboard`. Configuration modules in `src/django` should stay lean and declarative.

## Testing Guidelines
Follow the directory structure when adding coverage—name Python tests `*_test.py` and scope fixtures to the closest `conftest.py`. Async agent behaviour should use `pytest.mark.asyncio`. End-to-end flows live in `tests/e2e`; keep them deterministic and tag long-running checks appropriately. Before opening a PR, run `just test`; if you modified UI behaviour or web components, add `npm test` to the checklist.

## Commit & Pull Request Guidelines
Recent history favours short, present-tense commit subjects ("Add ops-control…"). Keep the subject under ~70 characters and expand on context in the body when needed. Ensure `pre-commit` passes locally before pushing. PRs should describe the change, call out test results (e.g., `just test`, `npm test`), and attach screenshots for UX updates. Mention deployment considerations whenever `just deploy` or infrastructure scripts change.
