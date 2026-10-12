"""
Deployment tools for nyxmon.

Provides command-line utilities for deploying nyxmon to staging or production.
"""

import contextlib
import gzip
import os
import re
import shutil
import subprocess
import sys
import zlib
from collections.abc import Mapping
from pathlib import Path


@contextlib.contextmanager
def working_directory(path):
    """Changes working directory and returns to previous on exit."""
    prev_cwd = Path.cwd().absolute()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(prev_cwd)


def _deploy(environment: str) -> None:
    """
    Use ansible-playbook to deploy the site to the specified environment.

    Args:
        environment: Either "staging" or "production"
    """
    # Find the deploy directory relative to the package
    # In a deployed package, this would be relative to the site root
    deploy_root = Path.cwd() / "deploy"
    if not deploy_root.exists():
        # Try to find it from the package location
        import nyxmon

        package_root = Path(nyxmon.__file__).parent.parent.parent
        deploy_root = package_root / "deploy"

    if not deploy_root.exists():
        raise FileNotFoundError(
            f"Could not find deploy directory at {deploy_root}. "
            "Make sure you're running this command from the project root."
        )

    with working_directory(deploy_root):
        subprocess.call(["ansible-playbook", "deploy.yml", "--limit", environment])


def deploy_staging() -> None:
    """
    Deploy nyxmon to the staging environment.
    """
    _deploy("staging")


def deploy_production() -> None:
    """
    Deploy nyxmon to the production environment.
    """
    _deploy("production")


def deploy_macos() -> None:
    """
    Deploy nyxmon to a macOS environment.

    Uses a separate playbook with launchd services instead of systemd.
    """
    deploy_root = Path.cwd() / "deploy"
    if not deploy_root.exists():
        # Try to find it from the package location
        import nyxmon

        package_root = Path(nyxmon.__file__).parent.parent.parent
        deploy_root = package_root / "deploy"

    if not deploy_root.exists():
        raise FileNotFoundError(
            f"Could not find deploy directory at {deploy_root}. "
            "Make sure you're running this command from the project root."
        )

    with working_directory(deploy_root):
        subprocess.call(["ansible-playbook", "macos_deploy.yml"])


def remove_macos() -> None:
    """
    Remove nyxmon from a macOS environment.

    Stops and unloads launchd services, removes service files,
    and optionally deletes the user account and home directory.
    """
    deploy_root = Path.cwd() / "deploy"
    if not deploy_root.exists():
        # Try to find it from the package location
        import nyxmon

        package_root = Path(nyxmon.__file__).parent.parent.parent
        deploy_root = package_root / "deploy"

    if not deploy_root.exists():
        raise FileNotFoundError(
            f"Could not find deploy directory at {deploy_root}. "
            "Make sure you're running this command from the project root."
        )

    with working_directory(deploy_root):
        subprocess.call(["ansible-playbook", "macos_remove.yml"])


class LocalRestoreError(Exception):
    """The local restore was refused or failed; the target database is intact."""


LOCAL_RESTORE_DB_ENV = "NYXMON_LOCAL_RESTORE_DB"
DEFAULT_LOCAL_RESTORE_DB = "nyxmon"
LOCAL_RESTORE_ROLE = "nyxmon"
_STAGING_SUFFIX = "_restore_staging"
_PREVIOUS_SUFFIX = "_restore_previous"
_TARGET_RE = re.compile(r"nyxmon(_[a-z0-9]+)*\Z")
_BACKUP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\.sql\.gz\Z")
_MAX_DB_NAME_LENGTH = 63 - len(_STAGING_SUFFIX)


def resolve_local_restore_target(environ: Mapping[str, str] | None = None) -> str:
    """Return the local database the restore may replace.

    The name comes from ``NYXMON_LOCAL_RESTORE_DB`` (default ``nyxmon``) and
    must be ``nyxmon`` or ``nyxmon_<suffix>``. Anything else is refused, so the
    helper can never drop a database that belongs to another project.
    """
    env = os.environ if environ is None else environ
    target = env.get(LOCAL_RESTORE_DB_ENV, "") or DEFAULT_LOCAL_RESTORE_DB
    if (
        not _TARGET_RE.match(target)
        or len(target) > _MAX_DB_NAME_LENGTH
        or target.endswith((_STAGING_SUFFIX, _PREVIOUS_SUFFIX))
    ):
        raise LocalRestoreError(
            f"Refusing to restore into {target!r}: {LOCAL_RESTORE_DB_ENV} must be "
            "'nyxmon' or 'nyxmon_<suffix>' (lowercase letters, digits, underscores)."
        )
    return target


def _run_checked(argv: list[str], *, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            argv,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
        )
    except OSError as exc:
        raise LocalRestoreError(f"Could not run {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        raise LocalRestoreError(
            f"{' '.join(argv)} failed with exit status {result.returncode}"
        )
    return result.stdout or ""


def _maintenance_sql(sql: str, *, capture: bool = False) -> str:
    return _run_checked(
        [
            "psql",
            "-X",
            "-q",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-d",
            "postgres",
            "-c",
            sql,
        ],
        capture=capture,
    )


def _database_exists(name: str) -> bool:
    # Names reaching this point matched _TARGET_RE, so they are plain identifiers.
    out = _maintenance_sql(
        f"SELECT 1 FROM pg_database WHERE datname = '{name}'", capture=True
    )
    return out.strip() == "1"


def _ensure_role(name: str) -> None:
    out = _maintenance_sql(
        f"SELECT 1 FROM pg_roles WHERE rolname = '{name}'", capture=True
    )
    if out.strip() != "1":
        _run_checked(["createuser", name])


def _validate_backup(backup_path: Path) -> None:
    """Decompress the whole archive once so truncation or corruption is caught
    before any database is touched."""
    try:
        with gzip.open(backup_path, "rb") as source:
            empty = True
            while source.read(1024 * 1024):
                empty = False
    except (OSError, EOFError, zlib.error) as exc:
        raise LocalRestoreError(
            f"Backup {backup_path} is not a readable gzip archive: {exc}"
        ) from exc
    if empty:
        raise LocalRestoreError(f"Backup {backup_path} is empty")


def _load_backup(backup_path: Path, database: str) -> None:
    argv = [
        "psql",
        "-X",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "--single-transaction",
        "-d",
        database,
    ]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE)
    except OSError as exc:
        raise LocalRestoreError(f"Could not run psql: {exc}") from exc
    read_error: Exception | None = None
    assert proc.stdin is not None
    try:
        with gzip.open(backup_path, "rb") as source:
            shutil.copyfileobj(source, proc.stdin)
    except BrokenPipeError:
        pass  # psql stopped early; its exit status below reports why
    except (OSError, EOFError, zlib.error) as exc:
        read_error = exc
        proc.kill()
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()
        returncode = proc.wait()
    if read_error is not None:
        raise LocalRestoreError(
            f"Reading backup {backup_path} failed: {read_error}"
        ) from read_error
    if returncode != 0:
        raise LocalRestoreError(
            f"Loading {backup_path} into {database} failed "
            f"(psql exit status {returncode})"
        )


def restore_local_database(backup_path: Path, target: str) -> None:
    """Replace the local ``target`` database with the dump in ``backup_path``.

    The dump is loaded into a staging database first (one transaction, stop on
    the first SQL error). ``target`` is only replaced, by renaming, once that
    load succeeded; every earlier failure raises ``LocalRestoreError`` and
    leaves ``target`` exactly as it was.
    """
    target = resolve_local_restore_target({LOCAL_RESTORE_DB_ENV: target})
    staging = f"{target}{_STAGING_SUFFIX}"
    previous = f"{target}{_PREVIOUS_SUFFIX}"

    _validate_backup(backup_path)
    if _database_exists(previous):
        raise LocalRestoreError(
            f"Database {previous} is left over from an earlier restore. Check it "
            f"and remove it with 'dropdb {previous}' before restoring again."
        )
    _ensure_role(LOCAL_RESTORE_ROLE)
    _run_checked(["dropdb", "--if-exists", staging])
    _run_checked(["createdb", staging])
    try:
        _load_backup(backup_path, staging)
    except LocalRestoreError:
        with contextlib.suppress(LocalRestoreError):
            _run_checked(["dropdb", "--if-exists", staging])
        raise

    had_target = _database_exists(target)
    if had_target:
        _maintenance_sql(f'ALTER DATABASE "{target}" RENAME TO "{previous}"')
    try:
        _maintenance_sql(f'ALTER DATABASE "{staging}" RENAME TO "{target}"')
    except LocalRestoreError:
        if had_target:
            _maintenance_sql(f'ALTER DATABASE "{previous}" RENAME TO "{target}"')
        raise
    if had_target:
        try:
            _run_checked(["dropdb", previous])
        except LocalRestoreError as exc:
            print(
                f"Restore succeeded, but the old database is still present as "
                f"{previous}: {exc}",
                file=sys.stderr,
            )


def _honcho_is_running() -> bool:
    import psutil

    for proc in psutil.process_iter(["pid", "name", "username"]):
        if proc.info["name"] is None or "python" not in proc.info["name"]:
            continue
        try:
            if "honcho" in " ".join(proc.cmdline()):
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            # ignore processes that we cannot observe
            pass
    return False


def _project_dir(name: str) -> Path:
    path = Path.cwd() / name
    if not path.exists():
        # Try to find it from the package location
        import nyxmon

        path = Path(nyxmon.__file__).parent.parent.parent / name
    return path


def _fetch_production_backup() -> Path:
    deploy_root = _project_dir("deploy")
    if not deploy_root.exists():
        raise LocalRestoreError(
            f"Could not find deploy directory at {deploy_root}. "
            "Make sure you're running this command from the project root."
        )
    with working_directory(deploy_root):
        try:
            output = subprocess.check_output(
                ["ansible-playbook", "backup_database.yml", "--limit", "production"],
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise LocalRestoreError(f"Fetching the backup failed: {exc}") from exc

    lines_with_backup = [line for line in output.split("\n") if "sql.gz" in line]
    if not lines_with_backup:
        raise LocalRestoreError("No backup file found in ansible output")
    parts = lines_with_backup[0].split('"')
    backup_file_name = parts[-2] if len(parts) >= 3 else ""
    if not _BACKUP_NAME_RE.match(backup_file_name):
        raise LocalRestoreError(
            f"Unexpected backup file name in ansible output: {backup_file_name!r}"
        )
    backup_path = _project_dir("backups") / backup_file_name
    if not backup_path.is_file():
        raise LocalRestoreError(f"Backup file {backup_path} was not fetched")
    return backup_path


def production_db_to_local() -> None:
    """
    Fetch a production database backup with ansible and restore it locally.

    The local target is ``nyxmon`` unless ``NYXMON_LOCAL_RESTORE_DB`` names
    another ``nyxmon_<suffix>`` database; any other name is refused before
    anything is fetched or changed. The dump is loaded into a staging database
    and only replaces the target after it loaded completely, so a failed
    restore exits non-zero and leaves the existing database untouched.

    Make sure only the database is running using:
      postgres -D databases/postgres
    """
    try:
        target = resolve_local_restore_target()
        if _honcho_is_running():
            raise LocalRestoreError(
                "Please stop honcho first and start a single postgres db with "
                "postgres -D databases/postgres"
            )
        backup_path = _fetch_production_backup()
        restore_local_database(backup_path, target)
    except LocalRestoreError as exc:
        raise SystemExit(f"db-from-production: {exc}") from exc
    print(f"Restored backup from {backup_path} into {target}")
