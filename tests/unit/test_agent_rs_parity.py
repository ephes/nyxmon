"""Helpers of the Rust agent parity harness (agent-rs/parity, PLAN.md 7.4).

The harness itself runs in CI's ``parity`` job (``just rs-parity``); these
tests pin the parts whose silent breakage would make it pass vacuously: the
notification line parsers against the real ``LoggingNotifier`` output, the
masks, and the request-index scripts of the fake servers.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType

from nyxmon.adapters.notification import LoggingNotifier
from nyxmon.domain import Check, Result, ResultStatus

PARITY = Path(__file__).resolve().parents[2] / "agent-rs" / "parity"


def _load(name: str) -> ModuleType:
    if str(PARITY) not in sys.path:
        sys.path.insert(0, str(PARITY))
    spec = importlib.util.spec_from_file_location(name, PARITY / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run_parity = _load("run_parity")
fakes = _load("fakes")


def test_python_alert_lines_are_recognised(caplog):
    check = Check(
        check_id=7,
        service_id=1,
        name="json metrics (7)",
        check_type="json-metrics",
        url="",
        data={},
    )
    result = Result(
        check_id=7,
        status=ResultStatus.WARNING,
        data={"error_type": "threshold_failed", "error_msg": "1 warning: $.cpu"},
    )
    caplog.set_level(logging.ERROR)
    LoggingNotifier().notify_check_failed(check, result)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    log = "\n".join(formatter.format(record) for record in caplog.records)
    assert run_parity.python_alerts(log) == {7: [("warning", "threshold_failed")]}


def test_rust_shadow_lines_are_recognised():
    log = (
        "2026-10-15 10:00:00,123 - nyxmon.adapters.notification - ERROR - "
        "notification check_id=0 status=error error_type=stale_processing_lease_batch\n"
        "2026-10-15 10:00:01,000 - nyxmon.entrypoints.cli - INFO - unrelated\n"
        "2026-10-15 10:00:02,000 - nyxmon.adapters.notification - ERROR - "
        "notification check_id=3 status=warning error_type=\n"
    )
    assert run_parity.rust_alerts(log) == {
        0: [("error", "stale_processing_lease_batch")],
        3: [("warning", "")],
    }


def test_masks_keep_keys_and_hide_only_documented_values():
    data = {
        "error_type": "timeout",
        "error_msg": "ReadTimeout('timed out')",
        "duration_ms": 12,
        "attempt": 1,
        "nested": {"last_at": 5, "subject": "[nyxmon] x", "token": "abc", "value": 3},
    }
    assert run_parity.mask(data, "http") == {
        "error_type": "timeout",
        "error_msg": "<P2>",
        "duration_ms": "<masked>",
        "attempt": 1,
        "nested": {
            "last_at": "<masked>",
            "subject": "<masked>",
            "token": "<masked>",
            "value": 3,
        },
    }
    # A nyxmon-authored message of the same error type stays compared.
    assert run_parity.mask({"error_type": "timeout", "error_msg": "x"}, "ping") == {
        "error_type": "timeout",
        "error_msg": "x",
    }


def test_scripts_answer_by_request_index_and_repeat_the_last_response():
    script = fakes.Script()
    script.set("/c/1", ["a", "b"])
    assert [script.next("/c/1") for _ in range(4)] == ["a", "b", "b", "b"]
    assert script.next("/unknown") is None


def test_http_fake_serves_its_script():
    http = fakes.HttpFake()
    try:
        http.script.set(
            "/c/1", [{"status": 200, "body": {"a": 1}}, {"status": 503, "body": "down"}]
        )
        url = f"http://127.0.0.1:{http.port}/c/1"
        with urllib.request.urlopen(url) as response:
            assert (response.status, response.read()) == (200, b'{"a": 1}')
        for _ in range(2):
            try:
                urllib.request.urlopen(url)
            except urllib.error.HTTPError as err:
                assert (err.code, err.read()) == (503, b"down")
            else:  # pragma: no cover - the assertion documents the failure
                raise AssertionError("expected 503")
    finally:
        http.close()


def test_incident_payload_counters_are_compared_and_timestamps_masked():
    view = run_parity.incident_view
    payload = '{"incident_type": "stale_processing_lease", "reclaimed_count": %d, "last_seen_at": %d}'
    assert view("k", 1, payload % (1, 100)) == view("k", 1, payload % (1, 200))
    assert view("k", 1, payload % (1, 100)) != view("k", 1, payload % (99, 100))
    assert view("k", 1, "{}") != view("k", 2, "{}")


def _check(check_id, disabled=False):
    return run_parity.Check(check_id, "c", "http", "", {}, disabled=disabled)


def test_alerts_are_compared_per_check_and_disabled_checks_never_alert():
    checks = [_check(1), _check(18, disabled=True)]
    alert = [("error", "http_error")]
    assert run_parity.alert_differences(checks, {1: alert}, {1: alert}) == []
    assert run_parity.alert_differences(checks, {1: alert}, {}) == [
        "check 1: notifications python=[('error', 'http_error')] rust=[]"
    ]
    # A disabled check alerting, on one side or both, is a difference.
    assert run_parity.alert_differences(checks, {}, {18: alert})
    assert run_parity.alert_differences(checks, {18: alert}, {18: alert}) == [
        "check 18 (disabled): notifications [('error', 'http_error')]"
    ]
    # Collector-scoped alerts (check_id 0) are compared too.
    assert run_parity.alert_differences(checks, {0: alert}, {})
