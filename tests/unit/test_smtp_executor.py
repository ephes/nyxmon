"""Unit tests for the SMTP check executor."""

import pytest

from nyxmon.adapters.runner.executors.smtp_executor import (
    SmtpCheckExecutor,
    SmtpSendError,
    SmtpSendResponse,
)
from nyxmon.domain import Check, CheckType, ResultStatus


def _build_check(**data) -> Check:
    """Helper to build a minimal SMTP check."""
    return Check(
        check_id=data.get("check_id", 1),
        service_id=1,
        name=data.get("name", "SMTP Test"),
        check_type=CheckType.SMTP,
        url=data.get("url", "smtp.example.com"),
        data=data.get(
            "config",
            {
                "host": "smtp.example.com",
                "from_addr": "monitor@example.com",
                "to_addr": "alerts@example.com",
                "username": "monitor@example.com",
                "password": "secret",
                "retries": 2,
                "retry_delay": 0,
            },
        ),
    )


class StubSmtpClient:
    """Test double for the SMTP client."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple] = []

    async def send_mail(self, config, message):
        self.calls.append((config, message))
        if not self.responses:
            raise AssertionError("StubSmtpClient received more calls than expected")

        action = self.responses.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    async def aclose(self):
        return None


class TestSmtpCheckExecutor:
    @pytest.mark.anyio
    async def test_successful_send_returns_ok(self):
        client = StubSmtpClient([SmtpSendResponse(code=250, message="ok")])
        executor = SmtpCheckExecutor(client=client)

        result = await executor.execute(_build_check())

        assert result.status == ResultStatus.OK
        assert result.data["response_code"] == 250
        assert result.data["attempts"] == 1
        assert result.data["subject"].startswith("[nyxmon]")
        assert len(client.calls) == 1

    @pytest.mark.anyio
    async def test_retries_on_temporary_failure(self):
        client = StubSmtpClient(
            [
                SmtpSendError(
                    "try again later",
                    code=451,
                    temporary=True,
                    error_type="temporary_failure",
                ),
                SmtpSendResponse(code=250, message="ok"),
            ]
        )
        executor = SmtpCheckExecutor(client=client)

        result = await executor.execute(
            _build_check(
                config={
                    "host": "smtp.example.com",
                    "from_addr": "a@b.com",
                    "to_addr": "c@d.com",
                    "retries": 2,
                    "retry_delay": 0,
                }
            )
        )

        assert result.status == ResultStatus.OK
        assert result.data["attempts"] == 2
        assert len(client.calls) == 2

    @pytest.mark.anyio
    async def test_permanent_failure_does_not_retry(self):
        client = StubSmtpClient(
            [
                SmtpSendError(
                    "mailbox unavailable",
                    code=550,
                    temporary=False,
                    error_type="smtp_error",
                )
            ]
        )
        executor = SmtpCheckExecutor(client=client)

        result = await executor.execute(_build_check())

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "smtp_error"
        assert result.data["attempts"] == 1
        assert len(client.calls) == 1

    @pytest.mark.anyio
    async def test_auth_error_surfaces_without_retry(self):
        client = StubSmtpClient(
            [
                SmtpSendError(
                    "authentication failed",
                    code=535,
                    temporary=False,
                    error_type="auth_error",
                )
            ]
        )
        executor = SmtpCheckExecutor(client=client)

        result = await executor.execute(_build_check())

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "auth_error"
        assert result.data["attempts"] == 1
        assert len(client.calls) == 1

    @pytest.mark.anyio
    async def test_configuration_errors_surface(self):
        client = StubSmtpClient([SmtpSendResponse(code=250, message="ok")])
        executor = SmtpCheckExecutor(client=client)

        result = await executor.execute(
            _build_check(config={"from_addr": "a@b.com", "to_addr": "c@d.com"})
        )

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "configuration_error"
        assert client.calls == []
