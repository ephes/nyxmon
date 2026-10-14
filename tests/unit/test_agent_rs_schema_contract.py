"""Schema contract of the Rust agent (agent-rs/PLAN.md section 2.4).

The Rust agent runs no DDL: it verifies the Django-migrated schema at startup
and every Rust store test runs against ``agent-rs/fixtures/schema/schema.sql``.
These tests migrate a temporary database and fail when the committed contract
files no longer match, so a migration cannot merge without regenerating them
and consciously updating the Rust contract.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

GENERATOR_PATH = (
    Path(__file__).resolve().parents[2] / "agent-rs" / "fixtures" / "generate_schema.py"
)
STALE_MESSAGE = "run `just rs-fixtures` and update the Rust schema contract"


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agent_rs_schema_generator", GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_committed_schema_contract_matches_the_migrations(
    generator: ModuleType,
) -> None:
    stale = generator.stale_files(generator.render())
    assert not stale, f"stale Rust schema contract {stale}: {STALE_MESSAGE}"


def _add_agent_column(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE check_notification_state "
            "ADD COLUMN muted_until INTEGER NOT NULL DEFAULT 0"
        )


def _record_new_migration(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO django_migrations (app, name, applied) "
            "VALUES ('nyxboard', '0014_simulated', '2026-01-01 00:00:00')"
        )


def test_contract_detects_a_migration_that_changes_an_agent_table(
    generator: ModuleType,
) -> None:
    rendered = generator.render(prepare=_add_agent_column)

    assert generator.stale_files(rendered) == ["agent_tables.json", "schema.sql"]
    assert "muted_until" in rendered["agent_tables.json"]


def test_contract_detects_any_new_migration(generator: ModuleType) -> None:
    rendered = generator.render(prepare=_record_new_migration)

    assert generator.stale_files(rendered) == ["schema.sql"]
