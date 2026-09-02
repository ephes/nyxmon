import logging

from nyxmon.entrypoints.cli import setup_logging


def test_setup_logging_silences_http_transport_request_lines(monkeypatch) -> None:
    configured: dict[str, object] = {}

    def fake_basic_config(**kwargs: object) -> None:
        configured.update(kwargs)

    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    monkeypatch.setattr(httpx_logger, "level", logging.NOTSET)
    monkeypatch.setattr(httpcore_logger, "level", logging.NOTSET)

    setup_logging("INFO")

    assert configured["level"] == logging.INFO
    assert httpx_logger.level == logging.WARNING
    assert httpcore_logger.level == logging.WARNING
