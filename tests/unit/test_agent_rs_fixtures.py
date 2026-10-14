"""Drift guard for the Rust agent's shared fixtures (agent-rs/PLAN.md 7.2).

The fixtures in ``agent-rs/fixtures`` are generated from the Python
implementation and pin the behaviour the Rust port must reproduce. A Python
change that alters any of them must regenerate the files (``just
rs-fixtures``), which then shows the Rust side what to change.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

GENERATOR_PATH = (
    Path(__file__).resolve().parents[2] / "agent-rs" / "fixtures" / "generate.py"
)


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agent_rs_fixture_generator", GENERATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_committed_fixtures_match_the_python_implementation(
    generator: ModuleType,
) -> None:
    stale = generator.check_fixtures()
    assert not stale, (
        f"stale Rust agent fixtures {stale}: run `just rs-fixtures` and update the "
        "Rust side to match"
    )


def test_drift_check_detects_a_hand_edited_fixture(
    generator: ModuleType, tmp_path: Path
) -> None:
    for name in generator.GENERATORS:
        shutil.copy(generator.FIXTURES_DIR / name, tmp_path / name)
    assert generator.check_fixtures(tmp_path) == []

    edited = tmp_path / "env.json"
    text = edited.read_text(encoding="utf-8")
    assert '"value": 5' in text
    edited.write_text(text.replace('"value": 5', '"value": 6', 1), encoding="utf-8")
    assert generator.check_fixtures(tmp_path) == ["env.json"]

    (tmp_path / "pyjson.json").unlink()
    assert generator.check_fixtures(tmp_path) == ["env.json", "pyjson.json"]
