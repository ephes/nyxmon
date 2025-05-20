"""
Deployment tools for nyxmon.

Provides command-line utilities for deploying nyxmon to staging or production.
"""

import contextlib
import os
import subprocess
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


def production_db_to_local() -> None:
    """
    Use ansible to create and fetch a database backup from production.

    Make sure only the database is running using:
      postgres -D databases/postgres
    """
    import psutil

    for proc in psutil.process_iter(["pid", "name", "username"]):
        if proc.info["name"] is None or "python" not in proc.info["name"]:
            continue
        try:
            cmdline = " ".join(proc.cmdline())
            if "honcho" in cmdline:
                print(
                    "Please stop honcho first and start a single postgres db with postgres -D databases/postgres"
                )
                return
        except psutil.AccessDenied:
            # ignore processes that we cannot observe
            pass

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
        output = subprocess.check_output(
            ["ansible-playbook", "backup_database.yml", "--limit", "production"],
            text=True,
        )

    lines_with_backup = [line for line in output.split("\n") if "sql.gz" in line]
    if not lines_with_backup:
        print("No backup file found in ansible output")
        return

    backup_file_name = lines_with_backup[0].split('"')[-2]

    # Find the backups directory
    backups_dir = Path.cwd() / "backups"
    if not backups_dir.exists():
        import nyxmon

        package_root = Path(nyxmon.__file__).parent.parent.parent
        backups_dir = package_root / "backups"

    backup_path = backups_dir / backup_file_name
    db_name = "nyxmon"  # Changed from homepage to nyxmon

    subprocess.call(["dropdb", db_name])
    subprocess.call(["createdb", db_name])
    subprocess.call(["createuser", db_name])

    command = f"gunzip -c {backup_path} | psql {db_name}"
    print(command)
    subprocess.call(command, shell=True)
    print(f"Restored backup from {backup_path}")
