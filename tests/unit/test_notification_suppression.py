"""Unit tests for notification suppression windows."""

from __future__ import annotations

import gzip
import json
from typing import Any

import anyio
import httpx
import pytest

from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.adapters.site_connectivity import SiteConnectivityConfig, SiteMode
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import Check, CheckResult, CheckType, Result, ResultStatus
from nyxmon.service_layer import handlers
from nyxmon.service_layer.notification_suppression import (
    MAX_SUPPRESSION_BODY_BYTES,
    MAX_SUPPRESSION_REDIRECTS,
    notification_suppression_details,
)
from nyxmon.service_layer.unit_of_work import UnitOfWork


class StubNotifier:
    def __init__(self) -> None:
        self.failed_notifications: list[tuple[Check, Result]] = []

    def notify_check_failed(self, check: Check, result: Result) -> None:
        self.failed_notifications.append((check, result))

    def notify_service_status_changed(self, service: Any, status: str) -> None:
        del service, status


class FakeHttpClient:
    """Serves ``payload`` (or raises ``exc``) through a real httpx client.

    The suppression fetch drives httpx's streaming and redirect API, so the
    fake is an ``httpx.MockTransport`` handler that records each request.
    """

    def __init__(
        self, payload: dict[str, Any] | None = None, exc: Exception | None = None
    ) -> None:
        self.payload = payload or {}
        self.exc = exc
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "timeout": request.extensions["timeout"]["read"],
                "auth": request.headers.get("authorization"),
            }
        )
        if self.exc:
            raise self.exc
        return httpx.Response(200, json=self.payload)


def _build_check(data: dict[str, Any]) -> Check:
    return Check(
        check_id=1,
        service_id=1,
        name="JSON metrics",
        check_type=CheckType.JSON_METRICS,
        url="https://example.test/metrics",
        data=data,
    )


def _suppression_config() -> dict[str, Any]:
    return {
        "notification_suppression": {
            "url": "https://example.test/maintenance",
            "timeout": 1.5,
            "reason": "scheduled_maintenance",
            "status_path": "$.last_status",
            "active_statuses": ["running"],
            "finished_epoch_path": "$.last_run_finished_epoch",
            "active_for_seconds": 900,
            "auth": {"username": "nyxmon", "password": "secret"},
        }
    }


_REAL_HTTPX_CLIENT = httpx.Client


def _patch_handler(monkeypatch, handler) -> None:
    def factory(**kwargs: Any) -> httpx.Client:
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(
        "nyxmon.service_layer.notification_suppression.httpx.Client", factory
    )


def _patch_client(monkeypatch, fake_client: FakeHttpClient) -> FakeHttpClient:
    _patch_handler(monkeypatch, fake_client.handler)
    return fake_client


def test_suppression_active_while_maintenance_is_running(monkeypatch) -> None:
    client = _patch_client(monkeypatch, FakeHttpClient({"last_status": "running"}))
    check = _build_check(_suppression_config())

    details = notification_suppression_details(check, now_epoch=1_000)

    assert details == {
        "reason": "scheduled_maintenance",
        "source_url": "https://example.test/maintenance",
        "source_status": "running",
    }
    assert client.requests[0]["url"] == "https://example.test/maintenance"
    assert client.requests[0]["timeout"] == 1.5
    assert client.requests[0]["auth"] is not None


def test_suppression_active_during_recently_finished_window(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        FakeHttpClient({"last_status": "success", "last_run_finished_epoch": 900}),
    )
    check = _build_check(_suppression_config())

    details = notification_suppression_details(check, now_epoch=1_000)

    assert details == {
        "reason": "scheduled_maintenance",
        "source_url": "https://example.test/maintenance",
        "source_status": "success",
        "finished_epoch": 900,
        "active_for_seconds": 900,
    }


def test_suppression_inactive_after_recently_finished_window(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        FakeHttpClient({"last_status": "success", "last_run_finished_epoch": 99}),
    )
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_suppression_inactive_when_endpoint_errors(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeHttpClient(exc=RuntimeError("offline")))
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_suppression_ignores_malformed_active_if(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeHttpClient({"last_status": "success"}))
    config = _suppression_config()
    config["notification_suppression"]["active_if"] = None
    config["notification_suppression"]["active_for_seconds"] = 0
    check = _build_check(config)

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_suppression_clamps_timeout(monkeypatch) -> None:
    client = _patch_client(monkeypatch, FakeHttpClient({"last_status": "running"}))
    config = _suppression_config()
    config["notification_suppression"]["timeout"] = 999
    check = _build_check(config)

    assert notification_suppression_details(check, now_epoch=1_000) is not None
    assert client.requests[0]["timeout"] == 30.0


def test_suppressed_result_is_persisted_without_notification(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    monkeypatch.setattr(
        handlers,
        "notification_suppression_details",
        lambda check: {"reason": "maintenance"},
    )
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    check = _build_check({})
    store.checks.add(check)
    result = Result(check_id=check.check_id, status=ResultStatus.ERROR, data={})

    handlers.add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
    )

    assert uow.store.results.list()[0].data["notification_suppressed"] == {
        "reason": "maintenance"
    }
    assert notifier.failed_notifications == []


def test_suppressed_result_breaks_failure_notification_streak(monkeypatch) -> None:
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    suppression_calls = iter([{"reason": "maintenance"}, None, None])
    monkeypatch.setattr(
        handlers,
        "notification_suppression_details",
        lambda check: next(suppression_calls),
    )
    store = InMemoryStore()
    uow = UnitOfWork(store=store)
    notifier = StubNotifier()
    check = _build_check({})
    store.checks.add(check)

    for _ in range(3):
        result = Result(check_id=check.check_id, status=ResultStatus.ERROR, data={})
        handlers.add_check_result(
            AddCheckResult(check_result=CheckResult(check=check, result=result)),
            uow,
            notifier,
        )

    assert len(uow.store.results.list()) == 3
    assert len(notifier.failed_notifications) == 1
    notified_result = notifier.failed_notifications[0][1]
    assert notified_result.result_id == 2


def _fresh_config(**overrides: Any) -> dict[str, Any]:
    """Suppression config guarded by the payload's own freshness field."""
    config = _suppression_config()
    config["notification_suppression"].update(
        {
            "status_path": "$.units.mail_offsite.service.active_state",
            "active_statuses": [],
            "active_if": [
                {
                    "path": "$.units.mail_offsite.service.active_state",
                    "op": "==",
                    "value": "activating",
                }
            ],
            "freshness_path": "$.meta.age_seconds",
            "freshness_max_seconds": 600,
        }
    )
    config["notification_suppression"].update(overrides)
    return config


def _frozen_payload(age_seconds: Any) -> dict[str, Any]:
    """A payload mid-run, whose own reported age is the variable under test."""
    return {
        "meta": {"age_seconds": age_seconds},
        "units": {"mail_offsite": {"service": {"active_state": "activating"}}},
    }


def test_suppression_active_when_the_payload_is_fresh(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeHttpClient(_frozen_payload(30)))

    details = notification_suppression_details(
        _build_check(_fresh_config()), now_epoch=1_000
    )

    assert details is not None
    assert details["reason"] == "scheduled_maintenance"


def test_stale_payload_never_suppresses(monkeypatch) -> None:
    """Regression: a frozen payload must not silence the staleness alert.

    The suppression source here is the same endpoint whose freshness the check
    asserts. When the payload froze mid-run, `active_state` stayed
    "activating" forever and every later critical - including the
    `$.meta.age_seconds` staleness critical that exists to report the freeze -
    was suppressed indefinitely. Suppression must fail open on a stale source.
    """
    _patch_client(monkeypatch, FakeHttpClient(_frozen_payload(3_618_791)))

    details = notification_suppression_details(
        _build_check(_fresh_config()), now_epoch=1_000
    )

    assert details is None


def test_suppression_fails_open_when_the_freshness_field_is_missing(
    monkeypatch,
) -> None:
    payload = _frozen_payload(30)
    del payload["meta"]
    _patch_client(monkeypatch, FakeHttpClient(payload))

    details = notification_suppression_details(
        _build_check(_fresh_config()), now_epoch=1_000
    )

    assert details is None


def test_suppression_fails_open_on_a_non_numeric_freshness_value(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeHttpClient(_frozen_payload("recently")))

    details = notification_suppression_details(
        _build_check(_fresh_config()), now_epoch=1_000
    )

    assert details is None


def test_suppression_fails_open_when_the_max_age_is_unusable(monkeypatch) -> None:
    _patch_client(monkeypatch, FakeHttpClient(_frozen_payload(30)))

    for bad in (
        0,
        -1,
        None,
        "soon",
        10**400,
        float("inf"),
        float("-inf"),
        float("nan"),
        True,
    ):
        details = notification_suppression_details(
            _build_check(_fresh_config(freshness_max_seconds=bad)),
            now_epoch=1_000,
        )
        assert details is None, f"max_age={bad!r} must not suppress"


def test_hostile_freshness_values_fail_open_without_raising(monkeypatch) -> None:
    """Regression: numeric-but-unusable ages must neither raise nor suppress.

    ``float()`` of an arbitrarily large JSON integer raises ``OverflowError``,
    which would abort result handling for the whole check. ``nan``, ``-inf``
    and negative ages compare as "not older than max_age" and would permit
    suppression from a value that carries no freshness information at all.
    """
    for hostile in (
        10**400,
        -1,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
    ):
        _patch_client(monkeypatch, FakeHttpClient(_frozen_payload(hostile)))

        details = notification_suppression_details(
            _build_check(_fresh_config()), now_epoch=1_000
        )

        assert details is None, f"age={hostile!r} must fail open"


def test_a_fresh_payload_still_suppresses_after_the_hardening(monkeypatch) -> None:
    """Zero and boundary ages are legitimate and keep suppressing."""
    for fresh in (0, 0.0, 599.9, 600):
        _patch_client(monkeypatch, FakeHttpClient(_frozen_payload(fresh)))

        details = notification_suppression_details(
            _build_check(_fresh_config()), now_epoch=1_000
        )

        assert details is not None, f"age={fresh!r} is fresh and must suppress"


def test_unguarded_configs_keep_their_existing_behaviour(monkeypatch) -> None:
    """Absent freshness_path, suppression behaves exactly as before."""
    _patch_client(monkeypatch, FakeHttpClient({"last_status": "running"}))

    details = notification_suppression_details(
        _build_check(_suppression_config()), now_epoch=1_000
    )

    assert details is not None
    assert details["reason"] == "scheduled_maintenance"


# ---------------------------------------------------------------------------
# Maintenance suppression must not disturb the site-connectivity hold
# ---------------------------------------------------------------------------
#
# ``with_streak_reset()`` used to rebuild the record from scratch, which zeroed
# ``held_since``. A single maintenance-suppressed sample during an outage then
# re-armed the whole hold budget and dropped the check from the observer's
# recheck set, so a check could be held indefinitely and never rechecked.


T0 = 1_700_000_000
MAX_HOLD = 600
RETRY = 300


class _StubSnapshot:
    """Minimal connectivity snapshot: it holds when told to."""

    mode = SiteMode.ENFORCE

    def __init__(self, *, holding: bool = True) -> None:
        self.holding = holding

    def hold_reason(
        self, dependency: Any, claim_started_at: int, now: int, *, stale_after: int
    ) -> dict[str, Any] | None:
        del dependency, claim_started_at, now, stale_after
        if not self.holding:
            return None
        return {"reason": "site_down", "paths": ["internet"], "state": "down"}

    def observed_reason(
        self, dependency: Any, claim_started_at: int, now: int, *, stale_after: int
    ) -> dict[str, Any] | None:
        del dependency, claim_started_at, now, stale_after
        return None

    def dependency_recovered(
        self, dependency: Any, claim_started_at: int, now: int, *, stale_after: int
    ) -> bool:
        del dependency, claim_started_at, now, stale_after
        return False


class _StubSiteState:
    def __init__(self, snapshot: _StubSnapshot) -> None:
        self._snapshot = snapshot
        self.config = SiteConnectivityConfig(
            mode=SiteMode.ENFORCE, max_hold_seconds=MAX_HOLD
        )

    def snapshot(self) -> _StubSnapshot:
        return self._snapshot


class _Clock:
    def __init__(self, start: int = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> None:
        self.now += seconds


def _dependent_check() -> Check:
    return Check(
        check_id=1,
        service_id=1,
        name="held check",
        check_type=CheckType.HTTP,
        url="https://example.test/health",
        data={"site_dependency": "internet"},
    )


def _held_fixture(monkeypatch, *, holding: bool = True):
    """A check that pages on its first failing sample, with a frozen clock."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400")
    monkeypatch.delenv("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", raising=False)
    clock = _Clock()
    monkeypatch.setattr(handlers, "current_epoch", clock)
    store = InMemoryStore()
    check = _dependent_check()
    store.checks.add(check)
    snapshot = _StubSnapshot(holding=holding)
    return store, UnitOfWork(store=store), StubNotifier(), check, clock, snapshot


def _submit(uow, notifier, check, site_state, *, data=None):
    result = Result(
        check_id=check.check_id, status=ResultStatus.ERROR, data=dict(data or {})
    )
    return handlers.add_check_result(
        AddCheckResult(check_result=CheckResult(check=check, result=result)),
        uow,
        notifier,
        site_state,
    )


def test_a_suppressed_sample_does_not_re_arm_the_hold_budget(monkeypatch) -> None:
    """An exhausted hold stays exhausted across a maintenance window."""
    store, uow, notifier, check, clock, snapshot = _held_fixture(monkeypatch)
    site_state = _StubSiteState(snapshot)
    suppressed = iter([None, {"reason": "maintenance"}, None])
    monkeypatch.setattr(
        handlers, "notification_suppression_details", lambda check: next(suppressed)
    )

    _submit(uow, notifier, check, site_state)
    assert notifier.failed_notifications == []
    assert store.checks.get_notification_state(1).held_since == T0

    # A maintenance window closes over the still-ongoing outage.
    clock.advance(MAX_HOLD + 1)
    _submit(uow, notifier, check, site_state)
    assert store.checks.get_notification_state(1).held_since == T0, (
        "the hold budget was re-armed by a sample that says nothing about the site"
    )
    assert notifier.failed_notifications == []

    # The budget is spent, so the next failing sample alerts on its own merits.
    clock.advance(1)
    _submit(uow, notifier, check, site_state)
    assert len(notifier.failed_notifications) == 1
    annotation = notifier.failed_notifications[0][1].data["site_connectivity"]
    assert annotation["held"] is False
    assert annotation["exhausted"] is True


def test_a_suppressed_sample_keeps_the_check_in_the_held_set(monkeypatch) -> None:
    """The observer's recheck obligation survives a maintenance window."""
    store, uow, notifier, check, clock, snapshot = _held_fixture(monkeypatch)
    site_state = _StubSiteState(snapshot)
    suppressed = iter([None, {"reason": "maintenance"}])
    monkeypatch.setattr(
        handlers, "notification_suppression_details", lambda check: next(suppressed)
    )

    _submit(uow, notifier, check, site_state)
    assert store.checks.count_held_checks() == 1

    clock.advance(10)
    _submit(uow, notifier, check, site_state)

    assert store.checks.count_held_checks() == 1
    held = anyio.run(store.checks.list_held_checks_async)
    assert [(entry.check_id, entry.held_since) for entry in held] == [(1, T0)]


def test_a_hold_still_wins_over_a_due_delivery_retry(monkeypatch) -> None:
    """Site holds are decided before the retry, for immediate alerts too."""
    store, uow, notifier, check, clock, snapshot = _held_fixture(
        monkeypatch, holding=False
    )
    monkeypatch.setenv("NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", str(RETRY))
    handlers._delivery_retry_from_value.cache_clear()
    monkeypatch.setattr(
        handlers, "notification_suppression_details", lambda check: None
    )
    site_state = _StubSiteState(snapshot)

    class _FailingNotifier(StubNotifier):
        def notify_check_failed(self, check: Check, result: Result) -> bool:
            super().notify_check_failed(check, result)
            return False

    notifier = _FailingNotifier()
    _submit(uow, notifier, check, site_state, data={"notification_immediate": True})
    assert len(notifier.failed_notifications) == 1
    assert store.checks.get_notification_state(1).attempt_at == T0

    # The dependency goes down before the retry is due.
    snapshot.holding = True
    clock.advance(RETRY)
    _submit(uow, notifier, check, site_state, data={"notification_immediate": True})

    assert len(notifier.failed_notifications) == 1, "a held sample was retried"
    state = store.checks.get_notification_state(1)
    assert state.held_since == T0 + RETRY
    assert state.attempt_at == T0

    handlers._delivery_retry_from_value.cache_clear()


# --- active_if rules fail open on a missing path or a type mismatch ---------


def _active_if_config(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "notification_suppression": {
            "url": "https://example.test/maintenance",
            "reason": "scheduled_maintenance",
            "active_if": [rule],
        }
    }


@pytest.mark.parametrize(
    ("payload", "rule"),
    [
        # The field disappeared from the payload: `!=` must not match.
        (
            {"units": {}},
            {"path": "$.units.backup.state", "op": "!=", "value": "inactive"},
        ),
        # The whole branch is gone.
        ({}, {"path": "$.units.backup.state", "op": "!=", "value": "inactive"}),
        # `== null` must not match an absent field.
        ({}, {"path": "$.x", "op": "==", "value": None}),
        # A list index past the end is missing too.
        ({"items": []}, {"path": "$.items.0", "op": "!=", "value": "ok"}),
        # A path through a scalar is missing.
        ({"units": "down"}, {"path": "$.units.backup", "op": "!=", "value": "x"}),
    ],
)
def test_active_if_rule_never_matches_a_missing_path(
    monkeypatch, payload: dict[str, Any], rule: dict[str, Any]
) -> None:
    _patch_client(monkeypatch, FakeHttpClient(payload))
    check = _build_check(_active_if_config(rule))

    assert notification_suppression_details(check, now_epoch=1_000) is None


@pytest.mark.parametrize(
    ("payload", "rule"),
    [
        ({"age": True}, {"path": "$.age", "op": "<=", "value": 24}),
        ({"state": 0}, {"path": "$.state", "op": "!=", "value": "inactive"}),
        ({"state": "1"}, {"path": "$.state", "op": "!=", "value": 1}),
        ({"ok": 1}, {"path": "$.ok", "op": "!=", "value": False}),
        ({"state": None}, {"path": "$.state", "op": "!=", "value": "inactive"}),
    ],
)
def test_active_if_rule_never_matches_across_json_types(
    monkeypatch, payload: dict[str, Any], rule: dict[str, Any]
) -> None:
    _patch_client(monkeypatch, FakeHttpClient(payload))
    check = _build_check(_active_if_config(rule))

    assert notification_suppression_details(check, now_epoch=1_000) is None


@pytest.mark.parametrize(
    ("payload", "rule"),
    [
        (
            {"units": {"backup": {"state": "activating"}}},
            {"path": "$.units.backup.state", "op": "!=", "value": "inactive"},
        ),
        ({"x": None}, {"path": "$.x", "op": "==", "value": None}),
        ({"overdue": 3.5}, {"path": "$.overdue", "op": "<=", "value": 24}),
        ({"known": True}, {"path": "$.known", "op": "==", "value": True}),
        ({"items": ["busy"]}, {"path": "$.items.0", "op": "==", "value": "busy"}),
    ],
)
def test_active_if_rule_still_matches_a_present_value(
    monkeypatch, payload: dict[str, Any], rule: dict[str, Any]
) -> None:
    _patch_client(monkeypatch, FakeHttpClient(payload))
    check = _build_check(_active_if_config(rule))

    details = notification_suppression_details(check, now_epoch=1_000)

    assert details is not None
    assert details["matched_rule"]["op"] == rule["op"]


# --- the suppression body is read with a byte cap ----------------------------


def test_suppression_reads_a_small_body_through_httpx(monkeypatch) -> None:
    _patch_handler(
        monkeypatch,
        lambda request: httpx.Response(200, json={"last_status": "running"}),
    )
    check = _build_check(_suppression_config())

    details = notification_suppression_details(check, now_epoch=1_000)

    assert details is not None
    assert details["source_status"] == "running"


def test_oversized_suppression_body_fails_open(monkeypatch) -> None:
    padding = "x" * (MAX_SUPPRESSION_BODY_BYTES + 1)
    body = json.dumps({"last_status": "running", "padding": padding}).encode()

    def chunks():
        for start in range(0, len(body), 16_384):
            yield body[start : start + 16_384]

    # No Content-Length: the cap must hold while streaming.
    _patch_handler(monkeypatch, lambda request: httpx.Response(200, content=chunks()))
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_declared_oversized_suppression_body_fails_open(monkeypatch) -> None:
    body = json.dumps(
        {"last_status": "running", "padding": "x" * MAX_SUPPRESSION_BODY_BYTES}
    ).encode()
    _patch_handler(monkeypatch, lambda request: httpx.Response(200, content=body))
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None


def test_redirect_bodies_are_never_read(monkeypatch) -> None:
    consumed: list[int] = []

    def endless_body():
        for _ in range(1_000):
            consumed.append(1)
            yield b"x" * 16_384

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/maintenance":
            return httpx.Response(
                302, headers={"Location": "/final"}, content=endless_body()
            )
        return httpx.Response(200, json={"last_status": "running"})

    _patch_handler(monkeypatch, handler)
    check = _build_check(_suppression_config())

    details = notification_suppression_details(check, now_epoch=1_000)

    assert details is not None
    assert details["source_status"] == "running"
    assert consumed == []


def test_cross_origin_redirect_drops_credentials(monkeypatch) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "example.test":
            return httpx.Response(
                302, headers={"Location": "https://elsewhere.test/status"}
            )
        return httpx.Response(200, json={"last_status": "running"})

    _patch_handler(monkeypatch, handler)
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is not None
    assert seen[0][0] == "example.test" and seen[0][1] is not None
    assert seen[1] == ("elsewhere.test", None)


def test_same_origin_redirect_keeps_credentials(monkeypatch) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if request.url.path == "/maintenance":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, json={"last_status": "running"})

    _patch_handler(monkeypatch, handler)
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is not None
    assert len(seen) == 2 and seen[0] is not None and seen[1] == seen[0]


def test_redirect_loop_fails_open(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/maintenance"})

    _patch_handler(monkeypatch, handler)
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None
    assert len(calls) == MAX_SUPPRESSION_REDIRECTS + 1


def test_compressed_suppression_body_is_refused(monkeypatch) -> None:
    seen: list[str | None] = []
    body = gzip.compress(json.dumps({"last_status": "running"}).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=body)

    _patch_handler(monkeypatch, handler)
    check = _build_check(_suppression_config())

    assert notification_suppression_details(check, now_epoch=1_000) is None
    assert seen == ["identity"]
