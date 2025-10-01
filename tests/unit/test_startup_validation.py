"""Tests for startup validation functionality."""

import logging

import pytest

from nyxmon.startup_validation import validate_check_types
from nyxmon.domain import Check, CheckType
from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.service_layer import UnitOfWork
from nyxmon.adapters.runner import AsyncCheckRunner
from anyio.from_thread import BlockingPortalProvider


pytestmark = pytest.mark.anyio


class TestStartupValidation:
    """Tests for check type validation on startup."""

    async def test_no_checks_logs_info(self, caplog):
        """When no checks exist, should log info and not warn."""
        store = InMemoryStore()
        uow = UnitOfWork(store=store)
        runner = AsyncCheckRunner(BlockingPortalProvider())

        with caplog.at_level(logging.INFO):
            await validate_check_types(uow, runner)

        assert "No checks found" in caplog.text
        assert "WARNING" not in caplog.text

    async def test_all_registered_types_logs_success(self, caplog):
        """When all checks have registered types, should log success."""
        store = InMemoryStore()
        uow = UnitOfWork(store=store)

        # Add checks with registered types
        with uow:
            uow.store.checks.add(
                Check(
                    check_id=1,
                    service_id=1,
                    name="HTTP Check",
                    check_type=CheckType.HTTP,
                    url="https://example.com",
                    data={},
                )
            )
            uow.store.checks.add(
                Check(
                    check_id=2,
                    service_id=1,
                    name="DNS Check",
                    check_type=CheckType.DNS,
                    url="example.com",
                    data={"expected_ips": ["192.0.2.1"]},
                )
            )
            uow.commit()

        runner = AsyncCheckRunner(BlockingPortalProvider())

        with caplog.at_level(logging.INFO):
            await validate_check_types(uow, runner)

        assert "Validated 2 check(s)" in caplog.text
        assert "all types registered" in caplog.text
        assert "WARNING" not in caplog.text

    async def test_unregistered_type_logs_warning(self, caplog):
        """When checks have unregistered types, should log detailed warnings."""
        store = InMemoryStore()
        uow = UnitOfWork(store=store)

        # Add check with unregistered type
        with uow:
            uow.store.checks.add(
                Check(
                    check_id=1,
                    service_id=1,
                    name="Unknown Check",
                    check_type="unknown_type",
                    url="https://example.com",
                    data={},
                )
            )
            uow.commit()

        runner = AsyncCheckRunner(BlockingPortalProvider())

        with caplog.at_level(logging.WARNING):
            await validate_check_types(uow, runner)

        # Should log multiple warnings
        assert "Found 1 check(s) with unregistered types" in caplog.text
        assert "unknown_type" in caplog.text
        assert "Check #1 'Unknown Check'" in caplog.text
        assert "Action required" in caplog.text

    async def test_multiple_unregistered_checks_all_logged(self, caplog):
        """Multiple checks with unregistered types should all be logged."""
        store = InMemoryStore()
        uow = UnitOfWork(store=store)

        # Add multiple checks with unregistered types
        with uow:
            uow.store.checks.add(
                Check(
                    check_id=1,
                    service_id=1,
                    name="Unknown Check 1",
                    check_type="unknown_type",
                    url="https://example.com",
                    data={},
                )
            )
            uow.store.checks.add(
                Check(
                    check_id=2,
                    service_id=1,
                    name="Unknown Check 2",
                    check_type="custom_type",
                    url="https://example.com",
                    data={},
                )
            )
            uow.commit()

        runner = AsyncCheckRunner(BlockingPortalProvider())

        with caplog.at_level(logging.WARNING):
            await validate_check_types(uow, runner)

        assert "Found 2 check(s) with unregistered types" in caplog.text
        assert "unknown_type" in caplog.text
        assert "custom_type" in caplog.text
        assert "Check #1 'Unknown Check 1'" in caplog.text
        assert "Check #2 'Unknown Check 2'" in caplog.text

    async def test_mixed_registered_and_unregistered_types(self, caplog):
        """Mix of registered and unregistered types should only warn about unregistered."""
        store = InMemoryStore()
        uow = UnitOfWork(store=store)

        # Add mix of registered and unregistered checks
        with uow:
            uow.store.checks.add(
                Check(
                    check_id=1,
                    service_id=1,
                    name="Good HTTP Check",
                    check_type=CheckType.HTTP,
                    url="https://example.com",
                    data={},
                )
            )
            uow.store.checks.add(
                Check(
                    check_id=2,
                    service_id=1,
                    name="Bad Check",
                    check_type="bad_type",
                    url="https://example.com",
                    data={},
                )
            )
            uow.store.checks.add(
                Check(
                    check_id=3,
                    service_id=1,
                    name="Good DNS Check",
                    check_type=CheckType.DNS,
                    url="example.com",
                    data={"expected_ips": ["192.0.2.1"]},
                )
            )
            uow.commit()

        runner = AsyncCheckRunner(BlockingPortalProvider())

        with caplog.at_level(logging.WARNING):
            await validate_check_types(uow, runner)

        # Should warn about 1 unregistered check
        assert "Found 1 check(s) with unregistered types" in caplog.text
        assert "bad_type" in caplog.text
        assert "Check #2 'Bad Check'" in caplog.text

        # Should NOT mention the good checks in warnings
        assert "Good HTTP Check" not in caplog.text
        assert "Good DNS Check" not in caplog.text
