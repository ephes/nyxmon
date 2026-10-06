"""TLS certificate verification for IMAP and SMTP checks."""

from __future__ import annotations

import imaplib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import anyio
import pytest

from nyxmon.adapters.runner.executors.imap_executor import (
    ImapCheckExecutor,
    ImapLibSession,
)
from nyxmon.adapters.runner.executors.smtp_executor import (
    SmtpCheckExecutor,
    SmtplibClient,
    SmtpSendError,
)
from nyxmon.domain import Check, CheckType, ResultStatus
from nyxmon.domain.imap_config import ImapCheckConfig
from nyxmon.domain.smtp_config import SmtpCheckConfig


def _assert_verifying(context: Any) -> None:
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def _assert_unverified(context: Any) -> None:
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


# --- IMAP -----------------------------------------------------------------


def _imap_config(**overrides: Any) -> ImapCheckConfig:
    data = {
        "host": "imap.example.com",
        "username": "user",
        "password": "secret",
        "search_subject": "[nyxmon]",
    }
    data.update(overrides)
    return ImapCheckConfig.from_dict(data)


class FakeImapConn:
    """Stands in for an imaplib connection after the TLS step."""

    instances: list["FakeImapConn"] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.starttls_kwargs: dict[str, Any] | None = None
        self.shutdown_called = False
        FakeImapConn.instances.append(self)

    def starttls(self, **kwargs: Any):
        self.starttls_kwargs = kwargs
        return "OK", [b"Begin TLS"]

    def login(self, *args: Any):
        return "OK", [b""]

    def select(self, *args: Any):
        return "OK", [b"1"]

    def logout(self):
        return "BYE", [b""]

    def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.fixture
def fake_imaplib(monkeypatch):
    FakeImapConn.instances = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImapConn)
    monkeypatch.setattr(imaplib, "IMAP4", FakeImapConn)
    return FakeImapConn


def _open_imap_session(config: ImapCheckConfig) -> None:
    async def _run() -> None:
        async with ImapLibSession("imap.example.com", config):
            pass

    anyio.run(_run)


def test_imap_implicit_tls_verifies_certificate_by_default(fake_imaplib) -> None:
    _open_imap_session(_imap_config(tls_mode="implicit"))

    (conn,) = fake_imaplib.instances
    _assert_verifying(conn.kwargs["ssl_context"])


def test_imap_starttls_verifies_certificate_by_default(fake_imaplib) -> None:
    _open_imap_session(_imap_config(tls_mode="starttls", port=143))

    (conn,) = fake_imaplib.instances
    assert "ssl_context" not in conn.kwargs
    assert conn.starttls_kwargs is not None
    _assert_verifying(conn.starttls_kwargs["ssl_context"])


@pytest.mark.parametrize("tls_mode", ["implicit", "starttls"])
def test_imap_verify_false_uses_unverified_context_and_warns(
    fake_imaplib, caplog, tls_mode: str
) -> None:
    with caplog.at_level(logging.WARNING):
        _open_imap_session(_imap_config(tls_mode=tls_mode, verify=False))

    (conn,) = fake_imaplib.instances
    context = (
        conn.kwargs["ssl_context"]
        if tls_mode == "implicit"
        else conn.starttls_kwargs["ssl_context"]
    )
    _assert_unverified(context)
    assert "verification disabled" in caplog.text
    assert "imap.example.com" in caplog.text


def test_imap_plain_mode_uses_no_tls(fake_imaplib) -> None:
    _open_imap_session(_imap_config(tls_mode="none", port=143))

    (conn,) = fake_imaplib.instances
    assert "ssl_context" not in conn.kwargs
    assert conn.starttls_kwargs is None


def test_imap_starttls_failure_closes_connection(monkeypatch, fake_imaplib) -> None:
    def _fail(self, **kwargs: Any):
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(FakeImapConn, "starttls", _fail)

    with pytest.raises(ssl.SSLCertVerificationError):
        _open_imap_session(_imap_config(tls_mode="starttls", port=143))

    (conn,) = fake_imaplib.instances
    assert conn.shutdown_called


def test_imap_certificate_failure_reports_tls_error_without_retry() -> None:
    calls = 0

    class FailingSession:
        async def __aenter__(self):
            nonlocal calls
            calls += 1
            raise ssl.SSLCertVerificationError("certificate verify failed")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    executor = ImapCheckExecutor(session_factory=lambda host, cfg: FailingSession())
    check = Check(
        check_id=1,
        service_id=1,
        name="IMAP",
        check_type=CheckType.IMAP,
        url="imap.example.com",
        data={
            "host": "imap.example.com",
            "username": "user",
            "password": "secret",
            "search_subject": "[nyxmon]",
            "retries": 2,
            "retry_delay": 0,
        },
    )

    result = anyio.run(executor.execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "tls_error"
    assert calls == 1


# --- SMTP -----------------------------------------------------------------


def _smtp_config(**overrides: Any) -> SmtpCheckConfig:
    data = {
        "host": "smtp.example.com",
        "from_addr": "monitor@example.com",
        "to_addr": "alerts@example.com",
        "username": "monitor@example.com",
        "password": "secret",
    }
    data.update(overrides)
    return SmtpCheckConfig.from_dict(data)


class FakeSmtp:
    """Stands in for smtplib.SMTP / SMTP_SSL."""

    instances: list["FakeSmtp"] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.starttls_kwargs: dict[str, Any] | None = None
        FakeSmtp.instances.append(self)

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def ehlo(self) -> None:
        return None

    def starttls(self, **kwargs: Any) -> None:
        self.starttls_kwargs = kwargs

    def login(self, *args: Any) -> None:
        return None

    def send_message(self, message: EmailMessage) -> dict:
        return {}


class FakeSmtpSsl(FakeSmtp):
    pass


@pytest.fixture
def fake_smtplib(monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtpSsl)
    return FakeSmtp


def _send(config: SmtpCheckConfig) -> None:
    message = EmailMessage()
    message["Subject"] = "test"
    SmtplibClient()._send_blocking(config, message)


def test_smtp_implicit_tls_verifies_certificate_by_default(fake_smtplib) -> None:
    _send(_smtp_config(tls="implicit", port=465))

    (client,) = fake_smtplib.instances
    assert isinstance(client, FakeSmtpSsl)
    _assert_verifying(client.kwargs["context"])


def test_smtp_starttls_verifies_certificate_by_default(fake_smtplib) -> None:
    _send(_smtp_config(tls="starttls"))

    (client,) = fake_smtplib.instances
    assert not isinstance(client, FakeSmtpSsl)
    assert client.starttls_kwargs is not None
    _assert_verifying(client.starttls_kwargs["context"])


@pytest.mark.parametrize("tls", ["implicit", "starttls"])
def test_smtp_verify_false_uses_unverified_context_and_warns(
    fake_smtplib, caplog, tls: str
) -> None:
    with caplog.at_level(logging.WARNING):
        _send(_smtp_config(tls=tls, verify=False))

    (client,) = fake_smtplib.instances
    context = (
        client.kwargs["context"]
        if tls == "implicit"
        else client.starttls_kwargs["context"]
    )
    _assert_unverified(context)
    assert "verification disabled" in caplog.text
    assert "smtp.example.com" in caplog.text


def test_smtp_plain_mode_uses_no_tls(fake_smtplib, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        _send(_smtp_config(tls="none", port=25, verify=False))

    (client,) = fake_smtplib.instances
    assert not isinstance(client, FakeSmtpSsl)
    assert client.starttls_kwargs is None
    assert "verification disabled" not in caplog.text


def test_smtp_certificate_failure_maps_to_tls_error(monkeypatch, fake_smtplib) -> None:
    def _fail(self, **kwargs: Any) -> None:
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(FakeSmtp, "starttls", _fail)

    with pytest.raises(SmtpSendError) as excinfo:
        _send(_smtp_config(tls="starttls"))

    assert excinfo.value.error_type == "tls_error"
    assert excinfo.value.temporary is False


def test_smtp_executor_reports_tls_error_without_retry(
    monkeypatch, fake_smtplib
) -> None:
    def _fail(self, **kwargs: Any) -> None:
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(FakeSmtp, "starttls", _fail)
    check = Check(
        check_id=1,
        service_id=1,
        name="SMTP",
        check_type=CheckType.SMTP,
        url="smtp.example.com",
        data={
            "host": "smtp.example.com",
            "from_addr": "monitor@example.com",
            "to_addr": "alerts@example.com",
            "retries": 2,
            "retry_delay": 0,
        },
    )

    result = anyio.run(SmtpCheckExecutor().execute, check)

    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "tls_error"
    assert result.data["attempts"] == 1


# --- Config parsing -------------------------------------------------------


def test_imap_config_verify_defaults_true_and_round_trips() -> None:
    assert _imap_config().verify is True

    config = _imap_config(verify=False)
    assert config.verify is False
    assert config.to_dict()["verify"] is False
    assert ImapCheckConfig.from_dict(config.to_dict()).verify is False


def test_smtp_config_verify_defaults_true() -> None:
    assert _smtp_config().verify is True
    assert _smtp_config(verify=None).verify is True
    assert _smtp_config(verify=False).verify is False


@pytest.mark.parametrize("value", ["false", 0, "no"])
def test_mail_configs_reject_non_boolean_verify(value: Any) -> None:
    with pytest.raises(ValueError, match="verify must be a boolean"):
        _imap_config(verify=value)
    with pytest.raises(ValueError, match="verify must be a boolean"):
        _smtp_config(verify=value)
