"""Unit tests for executor registry and check executors."""

import pytest
from unittest.mock import Mock, AsyncMock
from nyxmon.adapters.runner.executors import (
    CheckExecutor,
    ExecutorRegistry,
    UnknownCheckTypeError,
)
from nyxmon.domain import Check, Result, ResultStatus, CheckType


class TestExecutorRegistry:
    """Tests for the executor registry."""

    def test_register_executor(self):
        """Should register an executor for a check type."""
        registry = ExecutorRegistry()
        mock_executor = Mock(spec=CheckExecutor)

        registry.register(CheckType.DNS, mock_executor)

        assert registry.get_executor(CheckType.DNS) == mock_executor

    def test_get_unregistered_executor_raises_error(self):
        """Should raise UnknownCheckTypeError for unregistered check type."""
        registry = ExecutorRegistry()

        with pytest.raises(UnknownCheckTypeError) as exc_info:
            registry.get_executor(CheckType.DNS)

        assert exc_info.value.check_type == CheckType.DNS
        assert "No executor registered" in str(exc_info.value)

    def test_register_multiple_executors(self):
        """Should handle multiple executor registrations."""
        registry = ExecutorRegistry()
        http_executor = Mock(spec=CheckExecutor)
        dns_executor = Mock(spec=CheckExecutor)

        registry.register(CheckType.HTTP, http_executor)
        registry.register(CheckType.DNS, dns_executor)

        assert registry.get_executor(CheckType.HTTP) == http_executor
        assert registry.get_executor(CheckType.DNS) == dns_executor

    def test_override_executor(self):
        """Should allow overriding an existing executor."""
        registry = ExecutorRegistry()
        executor1 = Mock(spec=CheckExecutor)
        executor2 = Mock(spec=CheckExecutor)

        registry.register(CheckType.DNS, executor1)
        registry.register(CheckType.DNS, executor2)

        assert registry.get_executor(CheckType.DNS) == executor2

    def test_list_registered_types(self):
        """Should list all registered check types."""
        registry = ExecutorRegistry()
        http_executor = Mock(spec=CheckExecutor)
        dns_executor = Mock(spec=CheckExecutor)

        registry.register(CheckType.HTTP, http_executor)
        registry.register(CheckType.DNS, dns_executor)

        registered_types = registry.list_registered_types()
        assert CheckType.HTTP in registered_types
        assert CheckType.DNS in registered_types
        assert len(registered_types) == 2


class TestCheckExecutorProtocol:
    """Tests for the CheckExecutor protocol compliance."""

    @pytest.mark.anyio
    async def test_executor_protocol_execute_method(self):
        """Should ensure executors follow the protocol."""
        # Create a mock executor that follows the protocol
        mock_executor = AsyncMock(spec=CheckExecutor)

        # Create a test check
        check = Check(
            check_id=1,
            service_id=1,
            name="Test DNS Check",
            check_type=CheckType.DNS,
            url="example.com",
            data={"expected_ips": ["192.168.1.1"]},
        )

        # Configure the mock to return a result
        expected_result = Result(
            check_id=1, status=ResultStatus.OK, data={"resolved_ips": ["192.168.1.1"]}
        )
        mock_executor.execute.return_value = expected_result

        # Execute and verify
        result = await mock_executor.execute(check)

        assert result == expected_result
        mock_executor.execute.assert_called_once_with(check)


class TestExecutorIntegration:
    """Integration tests for executor registry with actual executor."""

    @pytest.mark.anyio
    async def test_registry_with_executor(self):
        """Should work with an actual executor implementation."""

        # Create a simple test executor
        class TestExecutor:
            async def execute(self, check: Check) -> Result:
                return Result(
                    check_id=check.check_id,
                    status=ResultStatus.OK,
                    data={"test": "passed"},
                )

        registry = ExecutorRegistry()
        test_executor = TestExecutor()

        registry.register("test", test_executor)

        # Create a test check
        check = Check(
            check_id=42,
            service_id=1,
            name="Test Check",
            check_type="test",
            url="test.com",
            data={},
        )

        # Get executor and execute
        executor = registry.get_executor("test")
        assert executor is not None

        result = await executor.execute(check)
        assert result.check_id == 42
        assert result.status == ResultStatus.OK
        assert result.data == {"test": "passed"}
