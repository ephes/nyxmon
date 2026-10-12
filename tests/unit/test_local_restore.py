"""Local production-restore helper: target allowlist and failure guarantees."""

import gzip
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from nyxmon.entrypoints import deployment

ROOT = Path(__file__).resolve().parents[2]


class FakePostgres:
    """Records every command and simulates just enough PostgreSQL state."""

    def __init__(self, databases: set[str]) -> None:
        self.databases = set(databases)
        self.roles: set[str] = set()
        self.commands: list[list[str]] = []
        self.loaded: dict[str, bytes] = {}
        self.fail: str | None = None
        self.load_returncode = 0

    # subprocess.run replacement
    def run(self, argv, **kwargs):
        self.commands.append(list(argv))
        returncode, stdout = 0, ""
        if self.fail is not None and self.fail in " ".join(argv):
            returncode = 1
        elif argv[0] == "dropdb":
            self.databases.discard(argv[-1])
        elif argv[0] == "createdb":
            self.databases.add(argv[-1])
        elif argv[0] == "createuser":
            self.roles.add(argv[-1])
        elif argv[0] == "psql":
            sql = argv[-1]
            if match := re.search(r"datname = '(\w+)'", sql):
                stdout = "1\n" if match.group(1) in self.databases else ""
            elif match := re.search(r"rolname = '(\w+)'", sql):
                stdout = "1\n" if match.group(1) in self.roles else ""
            elif match := re.search(r'ALTER DATABASE "(\w+)" RENAME TO "(\w+)"', sql):
                old, new = match.groups()
                assert old in self.databases and new not in self.databases
                self.databases.remove(old)
                self.databases.add(new)
            else:  # pragma: no cover - guards the fake itself
                raise AssertionError(f"unexpected sql: {sql}")
        else:  # pragma: no cover - guards the fake itself
            raise AssertionError(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout)

    # subprocess.Popen replacement (the psql load)
    def popen(self, argv, **kwargs):
        self.commands.append(list(argv))
        fake = self

        class Proc:
            def __init__(self) -> None:
                self.stdin = _Sink()
                self.killed = False

            def kill(self) -> None:
                self.killed = True

            def wait(self) -> int:
                fake.loaded[argv[-1]] = self.stdin.data
                return 1 if self.killed else fake.load_returncode

        return Proc()

    def mutations(self) -> list[list[str]]:
        return [
            c
            for c in self.commands
            if c[0] in {"dropdb", "createdb", "createuser"}
            or "ALTER DATABASE" in c[-1]
            or "--single-transaction" in c
        ]


class _Sink:
    def __init__(self) -> None:
        self.data = b""

    def write(self, chunk: bytes) -> int:
        self.data += chunk
        return len(chunk)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_pg(monkeypatch):
    fake = FakePostgres({"nyxmon", "homepage"})
    monkeypatch.setattr(deployment.subprocess, "run", fake.run)
    monkeypatch.setattr(deployment.subprocess, "Popen", fake.popen)
    monkeypatch.setattr(deployment, "_honcho_is_running", lambda: False)
    monkeypatch.delenv(deployment.LOCAL_RESTORE_DB_ENV, raising=False)
    return fake


@pytest.fixture
def backup(tmp_path) -> Path:
    path = tmp_path / "2026-01-01-00:00:00_nyxmon.sql.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"CREATE TABLE restored (id integer);\n")
    return path


def _use_backup(monkeypatch, path: Path) -> list[int]:
    calls: list[int] = []

    def fetch() -> Path:
        calls.append(1)
        return path

    monkeypatch.setattr(deployment, "_fetch_production_backup", fetch)
    return calls


def test_default_restore_only_touches_nyxmon_databases(fake_pg, backup, monkeypatch):
    _use_backup(monkeypatch, backup)

    deployment.production_db_to_local()

    assert fake_pg.databases == {"nyxmon", "homepage"}
    assert fake_pg.loaded == {
        "nyxmon_restore_staging": b"CREATE TABLE restored (id integer);\n"
    }
    for command in fake_pg.commands:
        assert "homepage" not in " ".join(command)
    load = next(c for c in fake_pg.commands if "--single-transaction" in c)
    assert load[load.index("-v") + 1] == "ON_ERROR_STOP=1"


def test_configured_nyxmon_target_is_the_only_one_replaced(
    fake_pg, backup, monkeypatch
):
    monkeypatch.setenv(deployment.LOCAL_RESTORE_DB_ENV, "nyxmon_dev")
    _use_backup(monkeypatch, backup)

    deployment.production_db_to_local()

    assert fake_pg.databases == {"nyxmon", "homepage", "nyxmon_dev"}
    assert set(fake_pg.loaded) == {"nyxmon_dev_restore_staging"}
    assert not any(c[0] == "dropdb" and c[-1] == "nyxmon" for c in fake_pg.commands)


@pytest.mark.parametrize(
    "target",
    [
        "homepage",
        "postgres",
        "nyxmonitor",
        "Nyxmon",
        "nyxmon-dev",
        "nyxmon; DROP DATABASE homepage",
        'nyxmon"',
        "nyxmon_",
        "nyxmon_restore_staging",
        "nyxmon_dev_restore_previous",
        "nyxmon_" + "a" * 60,
    ],
)
def test_unrelated_target_is_rejected_before_any_work(fake_pg, monkeypatch, target):
    monkeypatch.setenv(deployment.LOCAL_RESTORE_DB_ENV, target)
    fetched = _use_backup(monkeypatch, Path("unused"))

    with pytest.raises(SystemExit) as excinfo:
        deployment.production_db_to_local()

    assert excinfo.value.code not in (0, None)
    assert fetched == []
    assert fake_pg.commands == []


def test_restore_function_rejects_unrelated_target(fake_pg, backup):
    with pytest.raises(deployment.LocalRestoreError):
        deployment.restore_local_database(backup, "homepage")
    assert fake_pg.commands == []


def test_invalid_gzip_fails_before_any_mutation(fake_pg, tmp_path, monkeypatch):
    broken = tmp_path / "broken.sql.gz"
    broken.write_bytes(b"this is not gzip")
    _use_backup(monkeypatch, broken)

    with pytest.raises(SystemExit) as excinfo:
        deployment.production_db_to_local()

    assert excinfo.value.code not in (0, None)
    assert fake_pg.mutations() == []
    assert fake_pg.databases == {"nyxmon", "homepage"}


def test_truncated_gzip_fails_before_any_mutation(fake_pg, backup, monkeypatch):
    backup.write_bytes(backup.read_bytes()[:-6])
    _use_backup(monkeypatch, backup)

    with pytest.raises(SystemExit):
        deployment.production_db_to_local()

    assert fake_pg.mutations() == []


def test_sql_error_preserves_target_and_drops_staging(fake_pg, backup, monkeypatch):
    fake_pg.load_returncode = 3
    _use_backup(monkeypatch, backup)

    with pytest.raises(SystemExit) as excinfo:
        deployment.production_db_to_local()

    assert excinfo.value.code not in (0, None)
    assert fake_pg.databases == {"nyxmon", "homepage"}
    assert not any("ALTER DATABASE" in c[-1] for c in fake_pg.commands)
    assert not any(c[0] == "dropdb" and c[-1] == "nyxmon" for c in fake_pg.commands)


def test_staging_creation_failure_preserves_target(fake_pg, backup, monkeypatch):
    fake_pg.fail = "createdb nyxmon_restore_staging"
    _use_backup(monkeypatch, backup)

    with pytest.raises(SystemExit) as excinfo:
        deployment.production_db_to_local()

    assert excinfo.value.code not in (0, None)
    assert fake_pg.databases == {"nyxmon", "homepage"}
    assert fake_pg.loaded == {}
    assert not any("ALTER DATABASE" in c[-1] for c in fake_pg.commands)


def test_failed_swap_puts_the_previous_database_back(fake_pg, backup, monkeypatch):
    fake_pg.fail = 'RENAME TO "nyxmon"'  # staging -> target fails once
    original_run = fake_pg.run

    def run(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if result.returncode:
            fake_pg.fail = None
        return result

    monkeypatch.setattr(deployment.subprocess, "run", run)
    _use_backup(monkeypatch, backup)

    with pytest.raises(SystemExit):
        deployment.production_db_to_local()

    assert "nyxmon" in fake_pg.databases
    assert "nyxmon_restore_previous" not in fake_pg.databases


def test_leftover_previous_database_is_never_overwritten(fake_pg, backup, monkeypatch):
    fake_pg.databases.add("nyxmon_restore_previous")
    _use_backup(monkeypatch, backup)

    with pytest.raises(SystemExit):
        deployment.production_db_to_local()

    assert fake_pg.mutations() == []


def test_running_honcho_exits_nonzero_without_fetching(fake_pg, monkeypatch):
    monkeypatch.setattr(deployment, "_honcho_is_running", lambda: True)
    fetched = _use_backup(monkeypatch, Path("unused"))

    with pytest.raises(SystemExit) as excinfo:
        deployment.production_db_to_local()

    assert excinfo.value.code not in (0, None)
    assert fetched == []


@pytest.mark.parametrize(
    "line",
    [
        '    "msg": "../../etc/passwd.sql.gz"',
        '    "msg": "/tmp/evil.sql.gz"',
        '    "msg": "a b.sql.gz"',
        "sql.gz without quotes",
    ],
)
def test_backup_name_from_ansible_output_is_validated(
    fake_pg, tmp_path, monkeypatch, line
):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "backups").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        deployment.subprocess, "check_output", lambda *a, **k: f"ok\n{line}\n"
    )

    with pytest.raises(deployment.LocalRestoreError):
        deployment._fetch_production_backup()


def test_fetch_resolves_backup_inside_backups_dir(fake_pg, tmp_path, monkeypatch):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "backups").mkdir()
    name = "2026-01-01-00:00:00_nyxmon.sql.gz"
    (tmp_path / "backups" / name).write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        deployment.subprocess,
        "check_output",
        lambda *a, **k: f'TASK\n    "msg": "{name}"\n',
    )

    assert deployment._fetch_production_backup() == tmp_path / "backups" / name


def test_legacy_commands_helper_has_no_unguarded_restore() -> None:
    source = (ROOT / "commands.py").read_text()
    assert '"homepage"' not in source
    assert "dropdb" not in source
    assert "shell=True" not in source
    assert "nyxmon.entrypoints.deployment import production_db_to_local" in source


# --- real PostgreSQL, opt-in: NYXMON_TEST_POSTGRES=1 -------------------------

requires_postgres = pytest.mark.skipif(
    os.environ.get("NYXMON_TEST_POSTGRES") != "1" or shutil.which("psql") is None,
    reason="set NYXMON_TEST_POSTGRES=1 to run against a local PostgreSQL",
)


def _psql(database: str, sql: str) -> str:
    return subprocess.run(
        ["psql", "-X", "-At", "-d", database, "-c", sql],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


@pytest.fixture
def pg_target(monkeypatch):
    # Tests must not create cluster-wide roles on the developer's PostgreSQL.
    monkeypatch.setattr(deployment, "_ensure_role", lambda name: None)
    target = f"nyxmon_test_{uuid.uuid4().hex[:10]}"
    yield target
    for name in (target, f"{target}_restore_staging", f"{target}_restore_previous"):
        subprocess.run(["dropdb", "--if-exists", name], check=True)


def _dump(path: Path, sql: str) -> Path:
    with gzip.open(path, "wb") as handle:
        handle.write(sql.encode())
    return path


@requires_postgres
def test_postgres_restore_replaces_existing_target(pg_target, tmp_path):
    subprocess.run(["createdb", pg_target], check=True)
    _psql(pg_target, "CREATE TABLE old_data (id integer)")
    dump = _dump(
        tmp_path / "ok.sql.gz",
        "CREATE TABLE restored (id integer);\nINSERT INTO restored VALUES (7);\n",
    )

    deployment.restore_local_database(dump, pg_target)

    assert _psql(pg_target, "SELECT id FROM restored") == "7"
    assert _psql(pg_target, "SELECT to_regclass('old_data') IS NULL") == "t"
    leftovers = _psql(
        "postgres",
        f"SELECT count(*) FROM pg_database WHERE datname LIKE '{pg_target}\\_restore%'",
    )
    assert leftovers == "0"


@requires_postgres
def test_postgres_sql_error_keeps_existing_target(pg_target, tmp_path):
    subprocess.run(["createdb", pg_target], check=True)
    _psql(pg_target, "CREATE TABLE old_data (id integer)")
    _psql(pg_target, "INSERT INTO old_data VALUES (1)")
    dump = _dump(
        tmp_path / "bad.sql.gz",
        "CREATE TABLE restored (id integer);\nTHIS IS NOT SQL;\n"
        "CREATE TABLE later (id integer);\n",
    )

    with pytest.raises(deployment.LocalRestoreError):
        deployment.restore_local_database(dump, pg_target)

    assert _psql(pg_target, "SELECT id FROM old_data") == "1"
    assert _psql(pg_target, "SELECT to_regclass('restored') IS NULL") == "t"
    leftovers = _psql(
        "postgres",
        f"SELECT count(*) FROM pg_database WHERE datname LIKE '{pg_target}\\_restore%'",
    )
    assert leftovers == "0"


@requires_postgres
def test_postgres_restore_creates_missing_target(pg_target, tmp_path):
    dump = _dump(tmp_path / "ok.sql.gz", "CREATE TABLE restored (id integer);\n")

    deployment.restore_local_database(dump, pg_target)

    assert _psql(pg_target, "SELECT to_regclass('restored') IS NOT NULL") == "t"
