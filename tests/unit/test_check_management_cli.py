"""Tests for the ``add-check`` console script."""

import json
import re
import shlex
import sqlite3
from pathlib import Path

import pytest

from nyxmon.adapters.repositories.sqlite_repo import (
    CheckIdExistsError,
    SqliteCheckRepository,
)
from nyxmon.domain import Check, CheckStatus
from nyxmon.entrypoints.check_management import (
    add_check_to_db,
    build_add_check_parser,
    parse_check_data,
)

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "nyxmon.db"
    path.touch()
    return path


def rows(db_path: Path) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id, name, check_type, url, check_interval, data "
            "FROM health_check ORDER BY id"
        ).fetchall()


def run_cli(db_path: Path, *args: str) -> None:
    add_check_to_db(["--db", str(db_path), "--service-id", "1", *args])


def test_calls_without_check_id_create_new_checks(db_path, capsys):
    dns_data = '{"expected_ips": ["192.0.2.1"]}'
    run_cli(
        db_path,
        "--name",
        "first",
        "--check-type",
        "dns",
        "--url",
        "example.com",
        "--data",
        dns_data,
    )
    run_cli(db_path, "--url", "https://example.org/")

    assert rows(db_path) == [
        (1, "first", "dns", "example.com", 300, dns_data),
        (2, "", "http", "https://example.org/", 300, "{}"),
    ]
    out = capsys.readouterr().out
    assert "added check ID 1" in out
    assert "added check ID 2" in out


def test_new_ids_follow_the_highest_existing_id(db_path):
    run_cli(db_path, "--url", "https://a.example/", "--check-id", "7")
    run_cli(db_path, "--url", "https://b.example/")

    assert [row[0] for row in rows(db_path)] == [7, 8]


def test_existing_check_id_is_refused_without_replace(db_path, capsys):
    run_cli(db_path, "--name", "keep", "--url", "https://keep.example/")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc_info:
        run_cli(db_path, "--url", "https://other.example/", "--check-id", "1")

    assert exc_info.value.code == 1
    assert "already exists" in capsys.readouterr().err
    assert rows(db_path) == [(1, "keep", "http", "https://keep.example/", 300, "{}")]


def test_replace_overwrites_an_existing_check(db_path):
    run_cli(db_path, "--name", "old", "--url", "https://old.example/")
    run_cli(
        db_path,
        "--name",
        "new",
        "--url",
        "https://new.example/",
        "--check-id",
        "1",
        "--replace",
    )

    assert rows(db_path) == [(1, "new", "http", "https://new.example/", 300, "{}")]


def test_replace_requires_check_id(db_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        run_cli(db_path, "--url", "https://x.example/", "--replace")

    assert exc_info.value.code == 2
    assert "--replace requires --check-id" in capsys.readouterr().err
    assert rows_or_empty(db_path) == []


@pytest.mark.parametrize(
    ("check_type", "data", "message"),
    [
        ("http", "{not json", "not valid JSON"),
        ("http", "[1, 2]", "must be a JSON object"),
        ("http", '"text"', "must be a JSON object"),
        ("dns", "{}", "expected_ips is required"),
        ("tcp", '{"port": "abc"}', "invalid --data for a tcp check"),
        ("json-metrics", '{"url": "https://x/", "checks": [1]}', "json-metrics"),
    ],
)
def test_invalid_data_is_rejected_before_writing(
    db_path, capsys, check_type, data, message
):
    with pytest.raises(SystemExit) as exc_info:
        run_cli(db_path, "--check-type", check_type, "--url", "x", "--data", data)

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err
    assert rows_or_empty(db_path) == []


def test_parse_check_data_defaults_to_empty_object():
    assert parse_check_data(None, "http") == {}


@pytest.mark.parametrize("value", ["0", "-3"])
def test_check_id_and_interval_must_be_positive(value):
    parser = build_add_check_parser()
    base = ["--db", "x", "--service-id", "1", "--url", "u"]
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--check-id", value])
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--interval", value])


def rows_or_empty(db_path: Path) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'health_check'"
        ).fetchone()
    return rows(db_path) if exists else []


def _documented_add_check_calls(doc: Path) -> list[list[str]]:
    blocks = re.findall(r"```bash\n(.*?)```", doc.read_text(), flags=re.S)
    calls = []
    for block in blocks:
        joined = block.replace("\\\n", " ")
        for command in re.findall(r"uv run add-check[^#]*?(?=\n\S|\Z)", joined):
            calls.append(shlex.split(command)[3:])
    return calls


@pytest.mark.parametrize("doc", ["dns-check-examples.md", "usage.md"])
def test_documented_examples_parse_and_validate(doc):
    calls = _documented_add_check_calls(DOCS_DIR / doc)
    assert calls, f"no add-check example found in {doc}"
    parser = build_add_check_parser()
    for argv in calls:
        args = parser.parse_args(argv)
        assert isinstance(parse_check_data(args.data, args.check_type), dict)


@pytest.mark.anyio
async def test_create_async_assigns_ids_and_refuses_existing(db_path):
    repo = SqliteCheckRepository(db_path)

    def make(url: str) -> Check:
        return Check(
            check_id=0,
            service_id=1,
            check_type="http",
            status=CheckStatus.IDLE,
            url=url,
            data={"timeout": 3},
        )

    first = await repo.create_async(make("https://one.example/"))
    second = await repo.create_async(make("https://two.example/"))
    assert (first, second) == (1, 2)

    with pytest.raises(CheckIdExistsError):
        await repo.create_async(make("https://three.example/"), check_id=1)

    stored = await repo._get_async(1)
    assert stored.url == "https://one.example/"
    assert stored.data == {"timeout": 3}
    assert json.loads(rows(db_path)[1][5]) == {"timeout": 3}
