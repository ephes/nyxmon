"""Startup validation utilities for nyxmon.

Validates system state on startup to catch configuration issues early.
"""

import logging
from typing import Set

from .adapters.runner import AsyncCheckRunner
from .service_layer import UnitOfWork


logger = logging.getLogger(__name__)


async def validate_check_types(uow: UnitOfWork, runner: AsyncCheckRunner) -> None:
    """Validate that all persisted checks have registered executors.

    This prevents runtime failures when the system attempts to execute
    checks with unregistered types after removing legacy fallback behavior.

    Args:
        uow: Unit of work for accessing persisted checks
        runner: Check runner with executor registry

    Logs warnings if checks with unregistered types are found.
    """
    checks = await uow.store.checks.list_async()

    if not checks:
        logger.info("No checks found in database - skipping check type validation")
        return

    # Get registered check types from the runner's executor registry
    registered_types: Set[str] = set(runner.executor_registry.list_registered_types())

    # Find checks with unregistered types
    unknown_checks = [c for c in checks if c.check_type not in registered_types]

    if unknown_checks:
        unknown_types = {c.check_type for c in unknown_checks}
        logger.warning(
            f"Found {len(unknown_checks)} check(s) with unregistered types: {unknown_types}"
        )
        logger.warning(
            f"These checks will fail when executed. Registered types: {registered_types}"
        )

        # Log details for each problematic check
        for check in unknown_checks:
            logger.warning(
                f"  - Check #{check.check_id} '{check.name}': "
                f"type='{check.check_type}' (unregistered)"
            )

        logger.warning(
            "Action required: Update or remove checks with unregistered types, "
            "or register executors for these types"
        )
    else:
        logger.info(
            f"Validated {len(checks)} check(s) - all types registered: {registered_types}"
        )
