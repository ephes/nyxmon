"""Tests for AsyncCheckRunner refactoring: resource scoping and error handling."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from nyxmon.adapters.runner.async_runner import AsyncCheckRunner
from nyxmon.domain import Check, CheckType, Result, ResultStatus


class TestAsyncRunnerResourceScoping:
    """Tests for conditional HTTP client creation."""

    @pytest.mark.anyio
    async def test_dns_only_batch_no_http_client_created(self):
        """DNS-only batches should not create HTTP client."""
        # Create a mock portal provider
        mock_portal = MagicMock()
        runner = AsyncCheckRunner(mock_portal)

        # Create DNS-only checks
        checks = [
            Check(
                check_id=1,
                service_id=1,
                name="DNS Check 1",
                check_type=CheckType.DNS,
                url="example.com",
                data={"expected_ips": ["192.0.2.1"]},
            ),
            Check(
                check_id=2,
                service_id=1,
                name="DNS Check 2",
                check_type=CheckType.DNS,
                url="example.org",
                data={"expected_ips": ["192.0.2.2"]},
            ),
        ]

        # Mock DNS executor to avoid actual DNS queries
        with patch(
            "nyxmon.adapters.runner.executors.dns_executor.DnsCheckExecutor.execute"
        ) as mock_dns_execute:
            mock_dns_execute.return_value = Result(
                check_id=1, status=ResultStatus.OK, data={"resolved_ips": ["192.0.2.1"]}
            )

            # Spy on httpx.AsyncClient to verify it's not created
            with patch("httpx.AsyncClient") as mock_client_class:
                results = []
                async for result in runner._async_run_all(checks):
                    results.append(result)

                # Verify HTTP client was NOT created
                mock_client_class.assert_not_called()

                # Verify DNS executor was called
                assert len(results) == 2

    @pytest.mark.anyio
    async def test_http_batch_creates_http_client(self):
        """HTTP batches should create HTTP client."""
        mock_portal = MagicMock()
        runner = AsyncCheckRunner(mock_portal)

        # Create HTTP checks
        checks = [
            Check(
                check_id=1,
                service_id=1,
                name="HTTP Check",
                check_type=CheckType.HTTP,
                url="https://example.com",
                data={},
            ),
        ]

        # Mock HTTP executor
        with patch(
            "nyxmon.adapters.runner.executors.http_executor.HttpCheckExecutor.execute"
        ) as mock_http_execute:
            mock_http_execute.return_value = Result(
                check_id=1, status=ResultStatus.OK, data={}
            )

            # Verify HTTP client is created
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_client_instance = AsyncMock()
                mock_client_class.return_value = mock_client_instance

                results = []
                async for result in runner._async_run_all(checks):
                    results.append(result)

                # Verify HTTP client was created
                mock_client_class.assert_called_once()
                # Verify client was closed
                mock_client_instance.aclose.assert_called_once()

    @pytest.mark.anyio
    async def test_mixed_batch_creates_http_client(self):
        """Mixed HTTP/DNS batches should create HTTP client."""
        mock_portal = MagicMock()
        runner = AsyncCheckRunner(mock_portal)

        # Create mixed checks
        checks = [
            Check(
                check_id=1,
                service_id=1,
                name="HTTP Check",
                check_type=CheckType.HTTP,
                url="https://example.com",
                data={},
            ),
            Check(
                check_id=2,
                service_id=1,
                name="DNS Check",
                check_type=CheckType.DNS,
                url="example.com",
                data={"expected_ips": ["192.0.2.1"]},
            ),
        ]

        # Mock executors
        with patch(
            "nyxmon.adapters.runner.executors.http_executor.HttpCheckExecutor.execute"
        ) as mock_http:
            with patch(
                "nyxmon.adapters.runner.executors.dns_executor.DnsCheckExecutor.execute"
            ) as mock_dns:
                mock_http.return_value = Result(
                    check_id=1, status=ResultStatus.OK, data={}
                )
                mock_dns.return_value = Result(
                    check_id=2, status=ResultStatus.OK, data={}
                )

                # Verify HTTP client is created for mixed batch
                with patch("httpx.AsyncClient") as mock_client_class:
                    mock_client_instance = AsyncMock()
                    mock_client_class.return_value = mock_client_instance

                    results = []
                    async for result in runner._async_run_all(checks):
                        results.append(result)

                    # Verify HTTP client was created (needed for HTTP check)
                    mock_client_class.assert_called_once()


class TestAsyncRunnerUnknownCheckType:
    """Tests for unknown check type error handling."""

    @pytest.mark.anyio
    async def test_unknown_check_type_returns_error_result(self):
        """Unknown check types should return an ERROR result, not raise exception."""
        mock_portal = MagicMock()
        runner = AsyncCheckRunner(mock_portal)

        # Create check with unknown type
        checks = [
            Check(
                check_id=1,
                service_id=1,
                name="Unknown Check",
                check_type="unknown_type",  # Not registered
                url="https://example.com",
                data={},
            ),
        ]

        # Should NOT raise exception, but return error result
        results = []
        async for result in runner._async_run_all(checks):
            results.append(result)

        # Verify we got an error result
        assert len(results) == 1
        result = results[0]
        assert result.check_id == 1
        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "unknown_check_type"
        assert "unknown_type" in result.data["error_msg"]
        assert result.data["check_type"] == "unknown_type"

    @pytest.mark.anyio
    async def test_unknown_check_type_does_not_crash_other_checks(self):
        """Unknown check types should not prevent other checks from running."""
        mock_portal = MagicMock()
        runner = AsyncCheckRunner(mock_portal)

        # Create mixed batch: valid DNS check + unknown type + valid HTTP check
        checks = [
            Check(
                check_id=1,
                service_id=1,
                name="DNS Check",
                check_type=CheckType.DNS,
                url="example.com",
                data={"expected_ips": ["192.0.2.1"]},
            ),
            Check(
                check_id=2,
                service_id=1,
                name="Legacy Custom Check",
                check_type="custom",  # Legacy type, not registered
                url="https://example.com",
                data={},
            ),
            Check(
                check_id=3,
                service_id=1,
                name="HTTP Check",
                check_type=CheckType.HTTP,
                url="https://example.com",
                data={},
            ),
        ]

        # Mock DNS and HTTP executors
        with patch(
            "nyxmon.adapters.runner.executors.dns_executor.DnsCheckExecutor.execute"
        ) as mock_dns:
            with patch(
                "nyxmon.adapters.runner.executors.http_executor.HttpCheckExecutor.execute"
            ) as mock_http:
                mock_dns.return_value = Result(
                    check_id=1,
                    status=ResultStatus.OK,
                    data={"resolved_ips": ["192.0.2.1"]},
                )
                mock_http.return_value = Result(
                    check_id=3, status=ResultStatus.OK, data={}
                )

                with patch("httpx.AsyncClient") as mock_client_class:
                    mock_client_instance = AsyncMock()
                    mock_client_class.return_value = mock_client_instance

                    results = []
                    async for result in runner._async_run_all(checks):
                        results.append(result)

        # Verify all 3 checks ran
        assert len(results) == 3

        # Sort results by check_id for predictable assertions
        results_by_id = {r.check_id: r for r in results}

        # DNS check should succeed
        assert results_by_id[1].status == ResultStatus.OK

        # Unknown type should return error (not crash)
        assert results_by_id[2].status == ResultStatus.ERROR
        assert results_by_id[2].data["error_type"] == "unknown_check_type"
        assert results_by_id[2].data["check_type"] == "custom"

        # HTTP check should succeed despite the unknown type in the batch
        assert results_by_id[3].status == ResultStatus.OK


class TestAsyncRunnerExecutorCleanup:
    """Tests for executor cleanup mechanism."""

    @pytest.mark.anyio
    async def test_executors_cleaned_up_after_batch(self):
        """Executors should be cleaned up after batch execution."""
        mock_portal = MagicMock()
        runner = AsyncCheckRunner(mock_portal)

        checks = [
            Check(
                check_id=1,
                service_id=1,
                name="HTTP Check",
                check_type=CheckType.HTTP,
                url="https://example.com",
                data={},
            ),
        ]

        # Mock HTTP executor with aclose
        with patch(
            "nyxmon.adapters.runner.executors.http_executor.HttpCheckExecutor.execute"
        ) as mock_execute:
            with patch(
                "nyxmon.adapters.runner.executors.http_executor.HttpCheckExecutor.aclose"
            ):
                mock_execute.return_value = Result(
                    check_id=1, status=ResultStatus.OK, data={}
                )

                results = []
                async for result in runner._async_run_all(checks):
                    results.append(result)

                # Verify executors were cleaned up
                # Note: aclose_all calls aclose on all instantiated executors
                assert len(results) == 1

    @pytest.mark.anyio
    async def test_cleanup_happens_even_on_error(self):
        """Executors should be cleaned up even if execution fails."""
        mock_portal = MagicMock()
        runner = AsyncCheckRunner(mock_portal)

        checks = [
            Check(
                check_id=1,
                service_id=1,
                name="HTTP Check",
                check_type=CheckType.HTTP,
                url="https://example.com",
                data={},
            ),
        ]

        # Mock executor to raise an exception
        with patch(
            "nyxmon.adapters.runner.executors.http_executor.HttpCheckExecutor.execute"
        ) as mock_execute:
            mock_execute.side_effect = RuntimeError("Test error")

            with patch("httpx.AsyncClient") as mock_client_class:
                mock_client_instance = AsyncMock()
                mock_client_class.return_value = mock_client_instance

                # Should handle the error - anyio will wrap it in ExceptionGroup
                with pytest.raises((RuntimeError, BaseExceptionGroup)):
                    results = []
                    async for result in runner._async_run_all(checks):
                        results.append(result)

                # Verify cleanup still happened
                mock_client_instance.aclose.assert_called_once()
