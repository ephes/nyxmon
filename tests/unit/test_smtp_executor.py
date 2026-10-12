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


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,value",
    [
        (field, value)
        for field in ("port", "retries", "timeout", "retry_delay")
        for value in (
            True,
            False,
            "587",
            [],
            {},
            None,
            float("nan"),
            float("inf"),
            float("-inf"),
        )
    ]
    + [
        ("port", 0),
        ("port", 65536),
        ("port", 587.5),
        ("retries", -1),
        ("retries", 1.5),
        ("timeout", 0),
        ("timeout", 301),
        ("retry_delay", -1),
        ("retry_delay", 301),
        ("timeout", 10**1000),
    ],
)
async def test_invalid_numeric_config_does_not_send(field, value):
    check = _build_check()
    check.data[field] = value
    client = StubSmtpClient([])
    result = await SmtpCheckExecutor(client).execute(check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert client.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["from_addr", "to_addr", "subject_prefix"])
@pytest.mark.parametrize(
    "value", ["a\rb", "a\nb", "a\x0bb", [], {}, 123, True, None, "", "   "]
)
async def test_invalid_header_config_does_not_send(field, value):
    check = _build_check()
    check.data[field] = value
    client = StubSmtpClient([])
    result = await SmtpCheckExecutor(client).execute(check)
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert client.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sender", ["Monitor <monitor@example.com>", '"ops@night" <monitor@example.com>']
)
async def test_display_name_headers_send(sender):
    check = _build_check()
    check.data.update(
        from_addr=sender,
        to_addr="Alerts <alerts@example.com>",
        port=65535,
        timeout=300,
        retry_delay=300,
        retries=0,
    )
    client = StubSmtpClient([SmtpSendResponse(250, "ok")])
    result = await SmtpCheckExecutor(client).execute(check)
    assert result.status == ResultStatus.OK
    assert str(client.calls[0][1]["From"]) == check.data["from_addr"]
    assert str(client.calls[0][1]["To"]) == check.data["to_addr"]
    message_id = client.calls[0][1]["Message-ID"]
    assert str(message_id).endswith("@example.com>")
    assert message_id.defects == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [ValueError("invalid header"), TypeError("invalid header")]
)
async def test_message_build_errors_are_configuration_errors(monkeypatch, error):
    client = StubSmtpClient([])
    executor = SmtpCheckExecutor(client)

    def fail(*args):
        raise error

    monkeypatch.setattr(executor, "_build_message", fail)
    result = await executor.execute(_build_check())
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert client.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "shutdown", [451, 500, "reply", "disconnect", "oserror", "close"]
)
@pytest.mark.parametrize("tls", ["none", "implicit"])
@pytest.mark.parametrize(
    "delivery", ["accepted", "refused", "all_refused", "temporary", "pre_data"]
)
async def test_shutdown_preserves_delivery_outcome(
    monkeypatch, shutdown, tls, delivery
):
    import smtplib
    from nyxmon.adapters.runner.executors import smtp_executor

    calls = {"send": 0, "quit": 0, "close": 0, "ehlo": 0}
    delays = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.quit()

        def ehlo(self):
            calls["ehlo"] += 1
            if delivery == "pre_data" and calls["ehlo"] == 1:
                raise smtplib.SMTPResponseException(451, b"try again")

        def send_message(self, message):
            calls["send"] += 1
            if delivery == "temporary" and calls["send"] == 1:
                raise smtplib.SMTPDataError(451, b"try again")
            if delivery == "all_refused":
                raise smtplib.SMTPRecipientsRefused(
                    {"alerts@example.com": (550, b"no recipient")}
                )
            if delivery == "refused":
                return {"alerts@example.com": (550, b"no recipient")}
            return {}

        def quit(self):
            calls["quit"] += 1
            if shutdown == "reply":
                return (500, b"quit failed")
            if shutdown == "disconnect":
                raise smtplib.SMTPServerDisconnected("quit disconnected")
            if shutdown in ("oserror", "close"):
                raise OSError("quit failed")
            raise smtplib.SMTPResponseException(shutdown, b"quit failed")

        def close(self):
            calls["close"] += 1
            if shutdown == "close":
                raise OSError("close failed")

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(smtp_executor.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_executor.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtp_executor.anyio, "sleep", fake_sleep)
    check = _build_check()
    check.data.update(tls=tls, username=None, retry_delay=5)
    result = await SmtpCheckExecutor().execute(check)
    if delivery in ("refused", "all_refused"):
        assert result.status == ResultStatus.ERROR
        if delivery == "refused":
            assert result.data["error_type"] == "recipient_refused"
            assert result.data["smtp_code"] == 550
        else:
            assert result.data["error_type"] == "smtp_error"
    else:
        assert result.status == ResultStatus.OK
    retried = delivery in ("temporary", "pre_data")
    assert calls["send"] == (2 if delivery == "temporary" else 1)
    assert result.data["attempts"] == (2 if retried else 1)
    assert delays == ([5] if retried else [])
    assert calls["quit"] == calls["close"] == (2 if retried else 1)
