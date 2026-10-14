"""Fixture builders of slice S3 (alerting core), loaded by ``generate.py``.

Every builder calls the real Python functions (PLAN.md section 7.2):

* ``policy.json``: ``resolve_notification_policy`` (check data x status x
  environment, warn-once sequences);
* ``site_dependency.json``: ``resolve_site_dependency`` and
  ``warn_unobserved_requirements``;
* ``site_snapshot.json``: ``SiteConnectivitySnapshot`` hold logic;
* ``suppression_rules.json``: ``_lookup``, ``_rule_matches``,
  ``_finite_seconds``, ``_int_or_none``, ``_timeout_seconds`` and
  ``notification_suppression_details`` with a stubbed fetch, plus the
  ``repr()``/``str()`` of JSON values the messages embed;
* ``decision.json``: ``_should_notify_check_result`` with a stub unit of
  work and site state; every line of the function must be executed by the
  corpus (asserted with ``sys.monitoring``);
* ``lease.json``: ``estimated_check_runtime_seconds``;
* ``incident_payloads.json``: the delivery-marker helpers of
  ``repositories/interface.py``.

JSON inputs and outputs whose key order matters are stored as JSON text
(``*_json``), because the fixture files themselves are written with sorted
keys. Each case starts from a clean warn-once state unless it is a
``calls`` sequence.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest import mock

NOW = 1_800_000_000


@contextmanager
def _captured() -> Iterator[list[list[str]]]:
    records: list[list[str]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                records.append([record.name, record.getMessage()])

    handler = _Handler(level=logging.WARNING)
    root = logging.getLogger("nyxmon")
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _reset() -> None:
    from nyxmon.adapters import site_connectivity
    from nyxmon.service_layer import notification_policy, site_dependency

    notification_policy._bounded_env_value.cache_clear()
    notification_policy.reset_policy_warning_state()
    site_dependency.reset_dependency_warning_state()
    site_connectivity._warned_unobserved_checks.clear()


def _dumps(value: Any) -> str:
    return json.dumps(value)


# ------------------------------------------------------------------ policy

POLICY_DATA: list[Any] = [
    {},
    None,
    [],
    "text",
    {"notification_policy": None},
    {"notification_policy": 5},
    {"notification_policy": [1, 2]},
    {"notification_policy": "x"},
    {"notification_policy": {}},
    {"notification_policy": {"consecutive_failures": 3}},
    {"notification_policy": {"consecutive_failures": 1}},
    {"notification_policy": {"consecutive_failures": 100}},
    {"notification_policy": {"consecutive_failures": 0}},
    {"notification_policy": {"consecutive_failures": 101}},
    {"notification_policy": {"consecutive_failures": -1}},
    {"notification_policy": {"consecutive_failures": 2.0}},
    {"notification_policy": {"consecutive_failures": True}},
    {"notification_policy": {"consecutive_failures": "3"}},
    {"notification_policy": {"consecutive_failures": None}},
    {"notification_policy": {"consecutive_failures": 10**30}},
    {"notification_policy": {"consecutive_failures": [3]}},
    {"notification_policy": {"consecutive_failures": {"n": "it's"}}},
    {"notification_policy": {"reminder_seconds": 60}},
    {"notification_policy": {"reminder_seconds": 59}},
    {"notification_policy": {"reminder_seconds": 2592000}},
    {"notification_policy": {"reminder_seconds": 2592001}},
    {"notification_policy": {"reminder_seconds": 3600.5}},
    {"notification_policy": {"reminder_seconds": float("nan")}},
    {"notification_policy": {"reminder_seconds": "\u00fc\n"}},
    {
        "notification_policy": {
            "consecutive_failures": 4,
            "reminder_seconds": 7200,
            "warning_consecutive_failures": 6,
            "warning_reminder_seconds": 86400,
        }
    },
    {
        "notification_policy": {
            "warning_consecutive_failures": 0,
            "warning_reminder_seconds": 10,
            "consecutive_failures": 5,
        }
    },
    {
        "notification_policy": {
            "warning_consecutive_failures": "x",
            "warning_reminder_seconds": False,
            "consecutive_failures": 0,
            "reminder_seconds": 1e9,
        }
    },
    {"notification_policy": {"warning_consecutive_failures": 7}},
    {"notification_policy": {"warning_reminder_seconds": 120}},
]

POLICY_ENVS: list[dict[str, str]] = [
    {},
    {
        "NYXMON_NOTIFY_CONSECUTIVE_FAILURES": "4",
        "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS": "120",
        "NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS": "600",
    },
    {
        "NYXMON_NOTIFY_CONSECUTIVE_FAILURES": "0",
        "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS": "x",
        "NYXMON_NOTIFY_REPEAT_FAILURES": "3",
    },
]


def _policy(check_id: int, data: Any, status: str) -> dict[str, Any]:
    from nyxmon.service_layer.notification_policy import resolve_notification_policy

    policy = resolve_notification_policy(
        SimpleNamespace(check_id=check_id, data=data), status
    )
    return {
        "consecutive_failures": policy.consecutive_failures,
        "reminder_seconds": policy.reminder_seconds,
    }


def _run_calls(
    calls: list[dict[str, Any]], run: Callable[[dict[str, Any]], Any]
) -> list[dict[str, Any]]:
    """Run ``calls`` in order on one warn-once state; record value + warnings."""
    _reset()
    results = []
    for call in calls:
        with mock.patch.dict(os.environ, call.get("env", {}), clear=True):
            with _captured() as warnings:
                value = run(call)
        results.append({**call, "value": value, "warnings": warnings})
    _reset()
    return results


def build_policy() -> dict[str, Any]:
    def run(call: dict[str, Any]) -> Any:
        return _policy(call["check_id"], json.loads(call["data_json"]), call["status"])

    scenarios = []
    for env in POLICY_ENVS:
        for data in POLICY_DATA:
            for status in ("error", "warning"):
                call = {
                    "env": env,
                    "check_id": 7,
                    "data_json": _dumps(data),
                    "status": status,
                }
                scenarios.append(_run_calls([call], run))
    bad = _dumps({"notification_policy": {"consecutive_failures": 0}})
    other = _dumps({"notification_policy": {"reminder_seconds": 1}})
    scenarios.append(
        _run_calls(
            [
                {"check_id": 1, "data_json": bad, "status": "error"},
                {"check_id": 1, "data_json": bad, "status": "warning"},
                {"check_id": 1, "data_json": other, "status": "error"},
                {"check_id": 2, "data_json": bad, "status": "error"},
                {
                    "check_id": 0,
                    "data_json": _dumps({"notification_policy": 1}),
                    "status": "error",
                },
                {
                    "check_id": 0,
                    "data_json": _dumps({"notification_policy": 2}),
                    "status": "error",
                },
            ],
            run,
        )
    )
    return {"scenarios": scenarios}


# -------------------------------------------------------- site dependency

DEPENDENCY_VALUES: list[Any] = [
    "internet",
    " Internet ",
    "INTERNET",
    "none",
    " NONE ",
    "",
    "   ",
    "intranet",
    "\u0130nternet",
    5,
    True,
    1.5,
    ["dns"],
    {},
    {"requires": None},
    {"requires": []},
    {"requires": "dns"},
    {"requires": {"dns": 1}},
    {"requires": ["dns"]},
    {"requires": ["ipv6"]},
    {"requires": ["DNS ", ["ipv4", "IPv6"], "dns", ["ipv4", "ipv6"], ["ipv6", "ipv4"]]},
    {"requires": [["dns", "dns", " DNS"]]},
    {"requires": [["ipv4"], "ipv4"]},
    {"requires": [[]]},
    {"requires": ["ipv5"]},
    {"requires": [["ipv4", 4]]},
    {"requires": [5]},
    {"requires": [None]},
    {"requires": [{"x": 1}]},
    {"requires": ["\u0130pv4"]},
    {"requires": ["ipv4\u00a0"]},
    {"requires": ["dns", ["ipv4", "ipv6"]], "extra": [1, 2.5, None]},
]


def _requirements(dependency: Any) -> Any:
    if dependency is None:
        return None
    return [list(requirement) for requirement in dependency.requirements]


def build_site_dependency() -> dict[str, Any]:
    from nyxmon.adapters.site_connectivity import warn_unobserved_requirements
    from nyxmon.service_layer.site_dependency import resolve_site_dependency

    def run(call: dict[str, Any]) -> Any:
        data = json.loads(call["data_json"])
        dependency = resolve_site_dependency(
            SimpleNamespace(check_id=call["check_id"], data=data)
        )
        if dependency is None:
            return None
        return {
            "requirements": _requirements(dependency),
            "path_names": list(dependency.path_names),
        }

    scenarios = []
    for data in [{}, None, [], "x", {"site_dependency": None}]:
        scenarios.append(_run_calls([{"check_id": 3, "data_json": _dumps(data)}], run))
    for value in DEPENDENCY_VALUES:
        data = {"site_dependency": value}
        scenarios.append(_run_calls([{"check_id": 3, "data_json": _dumps(data)}], run))
    bad = _dumps({"site_dependency": "bogus"})
    scenarios.append(
        _run_calls(
            [
                {"check_id": 1, "data_json": bad},
                {"check_id": 1, "data_json": _dumps({"site_dependency": 5})},
                {"check_id": 2, "data_json": bad},
                {"check_id": 2, "data_json": _dumps({"site_dependency": "internet"})},
                {"check_id": 0, "data_json": bad},
            ],
            run,
        )
    )

    def run_unobserved(call: dict[str, Any]) -> Any:
        checks = [
            SimpleNamespace(check_id=check_id, data=json.loads(data))
            for check_id, data in call["checks"]
        ]
        warn_unobserved_requirements(checks, call["unobserved"])
        return None

    internet = _dumps({"site_dependency": "internet"})
    v6 = _dumps({"site_dependency": {"requires": ["ipv6"]}})
    mixed = _dumps({"site_dependency": {"requires": ["ipv6", "dns", ["ipv4"]]}})
    group = _dumps({"site_dependency": {"requires": [["ipv4", "ipv6"]]}})
    bogus = _dumps({"site_dependency": "bogus"})
    checks = [[1, internet], [2, v6], [3, mixed], [4, group], [5, bogus], [6, "{}"]]
    unobserved_calls = [
        {"checks": checks, "unobserved": []},
        {"checks": checks, "unobserved": ["ipv6"]},
        {"checks": checks, "unobserved": ["ipv6", "dns", "ipv4"]},
        {"checks": checks, "unobserved": ["ipv6"]},
        {"checks": [[2, v6], [7, v6]], "unobserved": ["ipv6"]},
    ]
    unobserved = _run_calls(unobserved_calls, run_unobserved)
    return {"scenarios": scenarios, "unobserved": unobserved}


# ----------------------------------------------------------- site snapshot

PATH_STATES = ["up", "failing", "down", "recovering", "unobserved"]

DEPENDENCIES: list[Any] = [
    "internet",
    {"requires": ["ipv6"]},
    {"requires": ["dns"]},
    {"requires": [["ipv4", "ipv6"]]},
    {"requires": ["ipv4", "ipv6"]},
    {"requires": [["ipv6", "ipv4"], "dns"]},
    {"requires": ["dns", "ipv4", "ipv6"]},
]


def _random_paths(rng: random.Random) -> dict[str, list[Any]]:
    paths: dict[str, list[Any]] = {}
    for name in ("dns", "ipv4", "ipv6"):
        if rng.random() < 0.1:
            continue
        state = rng.choice(PATH_STATES)
        stamp = lambda: rng.choice([0, 0, NOW - 3000, NOW - 600, NOW - 60, NOW, -5])  # noqa: E731
        paths[name] = [state, stamp(), stamp(), stamp(), stamp()]
    return paths


def _snapshot(spec: dict[str, Any]) -> Any:
    from nyxmon.adapters.site_connectivity import (
        PathSnapshot,
        SiteConnectivitySnapshot,
        SiteMode,
    )

    return SiteConnectivitySnapshot(
        paths={name: PathSnapshot(*values) for name, values in spec["paths"].items()},
        observed_at=spec["observed_at"],
        mode=SiteMode(spec["mode"]),
        incident_id=spec["incident_id"],
    )


def _reason_json(reason: Any) -> Any:
    return None if reason is None else _dumps(reason)


def build_site_snapshot() -> dict[str, Any]:
    from nyxmon.service_layer.site_dependency import resolve_site_dependency

    rng = random.Random(3)
    specs: list[dict[str, Any]] = [
        {
            "paths": {
                "dns": ["up", 0, 0, 0, NOW - 100],
                "ipv4": ["down", NOW - 500, 0, 0, NOW - 4000],
                "ipv6": ["recovering", NOW - 900, NOW - 50, NOW + 850, 0],
            },
            "observed_at": NOW - 10,
            "mode": "enforce",
            "incident_id": NOW - 900,
        },
        {
            "paths": {
                "dns": ["unobserved", 0, 0, 0, 0],
                "ipv4": ["unobserved", 0, 0, 0, 0],
            },
            "observed_at": NOW,
            "mode": "enforce",
            "incident_id": 0,
        },
        {
            "paths": {
                "dns": ["up", 0, 0, 0, NOW - 20],
                "ipv4": ["failing", 0, 0, 0, NOW - 30],
                "ipv6": ["up", 0, 0, 0, NOW - 40],
            },
            "observed_at": NOW - 180,
            "mode": "observe",
            "incident_id": 0,
        },
    ]
    for _ in range(140):
        specs.append(
            {
                "paths": _random_paths(rng),
                "observed_at": rng.choice(
                    [NOW, NOW - 30, NOW - 180, NOW - 181, NOW + 5]
                ),
                "mode": rng.choice(["off", "observe", "enforce", "enforce"]),
                "incident_id": rng.choice([0, NOW - 3000]),
            }
        )
    cases = []
    for index, spec in enumerate(specs):
        snapshot = _snapshot(spec)
        for raw in [None, *DEPENDENCIES]:
            data = {} if raw is None else {"site_dependency": raw}
            dependency = resolve_site_dependency(data)
            claims = [0, -1, NOW - 4000, NOW - 600, NOW - 59, NOW + 1]
            for claim in claims if index < 3 else [rng.choice(claims)]:
                kwargs = {"stale_after": 180}
                cases.append(
                    {
                        "snapshot": spec,
                        "dependency_json": _dumps(raw),
                        "claim_started_at": claim,
                        "now": NOW,
                        "stale_after": 180,
                        "hold_reason_json": _reason_json(
                            snapshot.hold_reason(dependency, claim, NOW, **kwargs)
                        ),
                        "observed_reason_json": _reason_json(
                            snapshot.observed_reason(dependency, claim, NOW, **kwargs)
                        ),
                        "dependency_recovered": snapshot.dependency_recovered(
                            dependency, claim, NOW, **kwargs
                        ),
                        "dependency_usable": snapshot.dependency_usable(dependency),
                    }
                )
    return {"cases": cases}


# ------------------------------------------------------------- suppression

LOOKUP_PAYLOADS: list[Any] = [
    {"a": {"b": [10, {"c": None}]}, "0": "zero", "$": "dollar", "": "empty"},
    [[1, 2], {"k": "v"}],
    None,
    "text",
]

LOOKUP_PATHS = [
    "$",
    "",
    ".",
    "$.a",
    "a",
    "$.a.b",
    "$.a.b.0",
    "$.a.b.1.c",
    "$.a.b.2",
    "$.a.b.-1",
    "$.a.b.00",
    "$.a.b.1.c.d",
    "$.0",
    "0",
    "0.1",
    "1.k",
    "$.$.a",
    "a.$.b",
    "a..b",
    "$$.a",
    "$.a.b." + "9" * 30,
    "$.a.b." + "1" * 4301,
    "$.missing",
    "$.a.b.0.x",
]


def _lookup_outcome(func: Callable[[], Any]) -> dict[str, Any]:
    from nyxmon.service_layer.notification_suppression import MISSING

    try:
        value = func()
    except Exception as exc:  # noqa: BLE001 - the class is the contract
        return {"error": type(exc).__name__}
    if value is MISSING:
        return {"missing": True}
    return {"missing": False, "value_json": _dumps(value)}


RULE_PAYLOAD = {
    "status": "running",
    "count": 3,
    "ratio": 0.5,
    "flag": True,
    "nothing": None,
    "items": [1, [2, 3]],
    "obj": {"x": 1},
    "nan": float("nan"),
    "big": 10**25,
}

RULE_VALUES: list[Any] = [
    "running",
    "idle",
    3,
    3.0,
    2,
    True,
    False,
    None,
    [1, [2, 3]],
    [1, [2, 4]],
    [1],
    {"x": 1},
    {"x": 1.0},
    float("nan"),
    10**25,
]


def build_suppression_rules() -> dict[str, Any]:
    from nyxmon.service_layer import notification_suppression as ns

    lookups = []
    for payload in LOOKUP_PAYLOADS:
        for path in LOOKUP_PATHS:
            lookups.append(
                {
                    "payload_json": _dumps(payload),
                    "path": path,
                    **_lookup_outcome(lambda: ns._lookup(payload, path)),
                }
            )

    rules: list[dict[str, Any]] = []
    for key in RULE_PAYLOAD:
        for op in ["<", "<=", ">", ">=", "==", "!="]:
            for value in RULE_VALUES:
                rules.append({"path": f"$.{key}", "op": op, "value": value})
    rules += [
        {"path": "$.missing", "op": "!=", "value": 1},
        {"path": "$.missing", "op": "==", "value": None},
        {"path": "$.nothing", "op": "==", "value": None},
        {"path": "$.nothing", "op": "=="},
        {"path": "$.status", "op": "=", "value": "running"},
        {"path": "$.status", "op": None, "value": "running"},
        {"path": "$.status", "op": ["=="], "value": "running"},
        {"path": None, "op": "==", "value": RULE_PAYLOAD},
        {"op": "!=", "value": {}},
        {"path": 0, "op": "==", "value": "running"},
        {"path": "$.items.1.0", "op": ">", "value": 1},
        {"path": "$.items." + "1" * 4301, "op": "==", "value": 1},
        {"path": "$.status", "op": " == ", "value": "running"},
    ]
    matches = []
    for rule in rules:
        outcome = _lookup_outcome(lambda: ns._rule_matches(RULE_PAYLOAD, rule))
        matches.append({"rule_json": _dumps(rule), **outcome})

    numbers: list[Any] = [
        None,
        True,
        False,
        0,
        -1,
        7,
        10**30,
        10**400,
        2**1024 - 2**970,
        2**1024 - 2**971,
        0.0,
        -0.0,
        2.5,
        -2.5,
        1e300,
        float("inf"),
        float("-inf"),
        float("nan"),
        "12",
        " 12 ",
        "1_000",
        "1.5",
        "1e3",
        "inf",
        "nan",
        "-3",
        "abc",
        "",
        "1" * 4301,
        [1],
        {"a": 1},
    ]
    scalars = []
    for value in numbers:
        finite = ns._finite_seconds(value)
        integer = ns._int_or_none(value)
        try:
            timeout = {"value": repr(ns._timeout_seconds(value))}
        except Exception as exc:  # noqa: BLE001
            timeout = {"error": type(exc).__name__}
        scalars.append(
            {
                "value_json": _dumps(value),
                "finite_seconds": None if finite is None else repr(finite),
                "int_or_none": None if integer is None else str(integer),
                "timeout_seconds": timeout,
            }
        )

    texts = [
        {"value_json": _dumps(value), "repr": repr(value), "str": str(value)}
        for value in [
            None,
            True,
            0,
            -12,
            10**30,
            1.0,
            1e16,
            float("nan"),
            float("-inf"),
            "it's",
            'say "hi"',
            "both ' and \"",
            "\u00fc\n\x00\u200b\U0001f600",
            [],
            {},
            [1, "a", None, [2.5, {"k": [True]}]],
            {"a": {"b": []}, "c'": "d"},
        ]
    ]

    return {
        "lookups": lookups,
        "rule_matches": matches,
        "scalars": scalars,
        "value_text": texts,
        "details": _suppression_details(),
    }


def _suppression_details() -> list[dict[str, Any]]:
    from nyxmon.domain.models import Check
    from nyxmon.service_layer import notification_suppression as ns

    payload = {
        "last_status": "running",
        "state": "idle",
        "last_run_finished_epoch": NOW - 100,
        "generated_age": 30,
        "nested": {"finished": str(NOW - 10), "age": 30.5, "flag": True},
        "list": [{"age": 5}],
    }
    base = {"url": " https://maint.example/status "}
    configs: list[Any] = [
        None,
        [],
        "x",
        {},
        {"url": ""},
        {"url": "   "},
        {"url": None},
        {"url": 0},
        {"url": 123},
        {"url": ["u"]},
        base,
        {**base, "active_statuses": ["idle"]},
        {**base, "active_statuses": "running"},
        {**base, "active_statuses": None},
        {**base, "active_statuses": [], "reason": "deploy"},
        {**base, "status_path": "$.state", "active_statuses": ["idle"], "reason": 5},
        {**base, "status_path": "", "active_statuses": ["idle"]},
        {**base, "status_path": "$.nested", "active_statuses": ["x"]},
        {
            **base,
            "active_statuses": [],
            "active_if": [{"path": "$.state", "op": "==", "value": "idle"}],
        },
        {
            **base,
            "active_statuses": [],
            "active_if": [
                "bad",
                {"path": "$.nested.age", "op": ">", "value": 30},
                {"path": "$.generated_age", "op": "<=", "value": 30},
            ],
            "reason": "",
        },
        {**base, "active_statuses": [], "active_if": {"path": "$.state"}},
        {**base, "active_statuses": [], "active_if": [{"path": "$.state", "op": "=="}]},
        {
            **base,
            "active_statuses": [],
            "active_if": [{"path": "$.list." + "1" * 4301, "op": "=="}],
        },
        {**base, "active_statuses": [], "active_for_seconds": 200},
        {**base, "active_statuses": [], "active_for_seconds": 100},
        {**base, "active_statuses": [], "active_for_seconds": 99},
        {**base, "active_statuses": [], "active_for_seconds": "200"},
        {**base, "active_statuses": [], "active_for_seconds": 200.9, "reason": "r"},
        {**base, "active_statuses": [], "active_for_seconds": 0},
        {**base, "active_statuses": [], "active_for_seconds": -5},
        {**base, "active_statuses": [], "active_for_seconds": None},
        {**base, "active_statuses": [], "active_for_seconds": 10**30},
        {
            **base,
            "active_statuses": [],
            "active_for_seconds": 60,
            "finished_epoch_path": "$.nested.finished",
        },
        {
            **base,
            "active_statuses": [],
            "active_for_seconds": 60,
            "finished_epoch_path": "$.state",
        },
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": 60},
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": 30},
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": 29},
        {**base, "freshness_path": " $.nested.age ", "freshness_max_seconds": "31"},
        {**base, "freshness_path": "$.nested.age", "freshness_max_seconds": 30.9},
        {**base, "freshness_path": "$.nested.flag", "freshness_max_seconds": 60},
        {**base, "freshness_path": "$.missing", "freshness_max_seconds": 60},
        {**base, "freshness_path": "$.state", "freshness_max_seconds": 60},
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": 0},
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": -1},
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": None},
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": 10**400},
        {**base, "freshness_path": "$.generated_age", "freshness_max_seconds": "x"},
        {**base, "freshness_path": "$.list.0.age", "freshness_max_seconds": 10},
        {**base, "freshness_path": "", "freshness_max_seconds": 1},
        {**base, "freshness_path": "$.list." + "1" * 4301, "freshness_max_seconds": 1},
        {**base, "timeout": 0},
        {**base, "timeout": -1},
        {**base, "timeout": 45},
        {**base, "timeout": "2.5"},
        {**base, "timeout": "x"},
        {**base, "timeout": None},
        {**base, "timeout": True},
        {**base, "timeout": float("nan")},
        {**base, "timeout": float("inf")},
        {**base, "timeout": 10**400},
        {**base, "auth": {"username": "u", "password": "p"}},
        {**base, "auth": {"username": "u", "password": 5}},
        {**base, "auth": {"username": "u"}},
        {**base, "auth": "u:p"},
    ]

    cases = []
    for config in configs:
        for fetch_ok in (True, False):
            data = {} if config is None else {"notification_suppression": config}
            requests: list[dict[str, Any]] = []

            def fetch(url: str, *, timeout: float, auth: Any) -> Any:
                requests.append({"url": url, "timeout": repr(timeout), "auth": auth})
                if not fetch_ok:
                    raise RuntimeError("fetch failed")
                return json.loads(json.dumps(payload))

            check = Check(
                check_id=1, service_id=1, check_type="http", url="u", data=data
            )
            with (
                mock.patch.object(ns, "_fetch_payload", fetch),
                mock.patch("httpx.BasicAuth", lambda user, password: [user, password]),
            ):
                try:
                    details = ns.notification_suppression_details(check, now_epoch=NOW)
                    outcome: dict[str, Any] = {"details_json": _reason_json(details)}
                except Exception as exc:  # noqa: BLE001
                    outcome = {"error": type(exc).__name__}
            cases.append(
                {
                    "data_json": _dumps(data),
                    "payload_json": _dumps(payload),
                    "fetch_ok": fetch_ok,
                    "now": NOW,
                    "requests": requests,
                    **outcome,
                }
            )
    return cases


# ---------------------------------------------------------------- decision

STATE_FIELDS = (
    "failure_count",
    "last_attempt_count",
    "last_immediate_at",
    "last_notified_at",
    "first_failure_at",
    "held_since",
    "attempt_seq",
    "attempt_at",
)


def _state(values: list[int]) -> Any:
    from nyxmon.adapters.repositories.interface import NotificationState

    return NotificationState(**dict(zip(STATE_FIELDS, values)))


def _state_list(state: Any) -> list[int]:
    return [getattr(state, name) for name in STATE_FIELDS]


def _run_decision(case: dict[str, Any]) -> dict[str, Any]:
    from nyxmon.adapters.site_connectivity import SiteConnectivityConfig, SiteMode
    from nyxmon.domain.models import Check, CheckResult, Result
    from nyxmon.service_layer import handlers

    state = _state(case["state"])
    check = Check(
        check_id=case["check_id"],
        service_id=1,
        check_type="http",
        url="https://example.invalid",
        processing_started_at=case["claim_started_at"],
        data=json.loads(case["check_data_json"]),
    )
    result = Result(
        check_id=case["check_id"],
        status=case["status"],
        data=json.loads(case["result_data_json"]),
    )
    check_result = CheckResult(
        check, result, force_notification=case["force_notification"]
    )
    uow = SimpleNamespace(
        store=SimpleNamespace(
            checks=SimpleNamespace(get_notification_state=lambda _check_id: state)
        )
    )
    site = case["site"]
    site_state: Any = None
    if site is not None:
        snapshot = None if site["snapshot"] is None else _snapshot(site["snapshot"])
        site_state = SimpleNamespace(snapshot=lambda: snapshot)
        if site["config"] is not None:
            site_state.config = SiteConnectivityConfig(
                mode=SiteMode.ENFORCE,
                probe_interval=site["config"]["probe_interval"],
                max_hold_seconds=site["config"]["max_hold_seconds"],
            )
    _reset()
    with (
        mock.patch.dict(os.environ, case["env"], clear=True),
        mock.patch.object(handlers, "current_epoch", lambda: case["now"] + 0.75),
        _captured() as warnings,
    ):
        decision = handlers._should_notify_check_result(
            check_result,
            uow,
            case["immediate_cooldown"],
            site_state=site_state,
            retry_seconds=case["retry_seconds"],
        )
    _reset()
    return {
        **case,
        "should_notify": decision.should_notify,
        "expected": _state_list(decision.expected),
        "next_state": _state_list(decision.next_state),
        "held": decision.held,
        "attempt_seq": decision.attempt_seq,
        "decision_now": decision.now,
        "annotated_data_json": _dumps(result.data),
        "warnings": warnings,
    }


def _decision_inputs() -> list[dict[str, Any]]:
    rng = random.Random(5)
    internet = {"site_dependency": "internet"}
    blocked_paths = {
        "dns": ["up", 0, 0, 0, 0],
        "ipv4": ["down", NOW - 500, 0, 0, 0],
        "ipv6": ["recovering", NOW - 700, NOW - 50, NOW + 800, 0],
    }
    released_paths = {
        "dns": ["up", 0, 0, 0, NOW - 30],
        "ipv4": ["up", 0, 0, 0, NOW - 30],
        "ipv6": ["unobserved", 0, 0, 0, 0],
    }

    def snapshot(paths: dict[str, Any], mode: str, observed_at: int = NOW - 10) -> Any:
        return {
            "paths": paths,
            "observed_at": observed_at,
            "mode": mode,
            "incident_id": NOW - 700,
        }

    sites: list[Any] = [
        None,
        {"snapshot": None, "config": None},
        {"snapshot": snapshot(blocked_paths, "enforce"), "config": None},
        {"snapshot": snapshot(blocked_paths, "observe"), "config": None},
        {"snapshot": snapshot(blocked_paths, "off"), "config": None},
        {"snapshot": snapshot(blocked_paths, "enforce", NOW - 1000), "config": None},
        {
            "snapshot": snapshot(blocked_paths, "enforce", NOW - 1000),
            "config": {"probe_interval": 600, "max_hold_seconds": 600},
        },
        {"snapshot": snapshot(released_paths, "enforce"), "config": None},
        {"snapshot": snapshot(released_paths, "observe"), "config": None},
        {"snapshot": snapshot({"dns": ["up", 0, 0, 0, 0]}, "enforce"), "config": None},
    ]
    check_datas: list[Any] = [
        {},
        internet,
        {"site_dependency": "bogus"},
        {"site_dependency": {"requires": ["ipv6"]}},
        {
            **internet,
            "notification_policy": {"consecutive_failures": 1, "reminder_seconds": 60},
        },
        {
            "notification_policy": {
                "consecutive_failures": 3,
                "warning_reminder_seconds": 120,
            }
        },
        {"notification_policy": "bad"},
        [],
    ]
    result_datas: list[Any] = [
        {"error_type": "http_error", "status_code": 500},
        {"error_type": "x", "notification_immediate": True},
        {"notification_immediate": 1, "site_connectivity": {"stale": True}},
        {"notification_immediate": 0},
        {"collector_internal": True, "error_type": "stale_processing_lease"},
        {"collector_internal": False},
        {"notification_suppressed": {"reason": "maintenance_active"}},
        {"notification_suppressed": {}},
        {"site_connectivity": "old", "error_msg": "boom"},
    ]
    stamps = [
        0,
        0,
        NOW - 30,
        NOW - 59,
        NOW - 60,
        NOW - 3600,
        NOW - 11000,
        NOW - 21600,
        NOW + 5,
        -3,
    ]
    envs: list[dict[str, str]] = [
        {},
        {"NYXMON_NOTIFY_CONSECUTIVE_FAILURES": "3"},
        {
            "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS": "60",
            "NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS": "x",
            "NYXMON_NOTIFY_REPEAT_FAILURES": "2",
        },
    ]

    cases: list[dict[str, Any]] = []
    for status in ("ok", "error", "warning", "unknown"):
        for force in (False, True):
            cases.append(_decision_case(status=status, force_notification=force))
    for index in range(1000):
        state = [
            rng.choice([0, 0, 1, 2, 5]),
            rng.choice([0, 1, 2]),
            rng.choice(stamps),
            rng.choice(stamps),
            rng.choice(stamps),
            rng.choice(stamps),
            rng.choice([0, 0, 3, 9]),
            rng.choice(stamps),
        ]
        cases.append(
            _decision_case(
                state=state,
                check_id=index % 7,
                status=rng.choice(["error", "error", "warning", "ok"]),
                force_notification=rng.random() < 0.03,
                check_data=rng.choice(check_datas),
                result_data=rng.choice(result_datas),
                claim_started_at=rng.choice([0, NOW - 600, NOW - 10, -1]),
                site=rng.choice(sites),
                env=rng.choice(envs),
                retry_seconds=rng.choice([0, 0, 60, 3600]),
                immediate_cooldown=rng.choice([3600, 60, 10**30]),
            )
        )
    return cases


def _decision_case(
    *,
    state: list[int] | None = None,
    check_id: int = 1,
    status: str = "error",
    force_notification: bool = False,
    check_data: Any = None,
    result_data: Any = None,
    claim_started_at: int = 0,
    site: Any = None,
    env: dict[str, str] | None = None,
    retry_seconds: int = 0,
    immediate_cooldown: int = 3600,
) -> dict[str, Any]:
    return {
        "state": state or [0] * 8,
        "check_id": check_id,
        "status": status,
        "force_notification": force_notification,
        "check_data_json": _dumps({} if check_data is None else check_data),
        "result_data_json": _dumps({} if result_data is None else result_data),
        "claim_started_at": claim_started_at,
        "site": site,
        "env": env or {},
        "retry_seconds": retry_seconds,
        "immediate_cooldown": immediate_cooldown,
        "now": NOW,
    }


@contextmanager
def _line_coverage(code: Any) -> Iterator[set[int]]:
    """Lines of ``code`` executed inside the block (``sys.monitoring``).

    coverage.py is not a dependency and would clash with an outer tracer
    when the drift test runs under one; ``sys.monitoring`` tools coexist.
    """
    monitoring = sys.monitoring
    tool = next(t for t in range(6) if monitoring.get_tool(t) is None)
    monitoring.use_tool_id(tool, "agent-rs fixture coverage")
    seen: set[int] = set()

    def on_line(executed: Any, line: int) -> None:
        if executed is code:
            seen.add(line)

    try:
        monitoring.register_callback(tool, monitoring.events.LINE, on_line)
        monitoring.set_local_events(tool, code, monitoring.events.LINE)
        yield seen
    finally:
        monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)


def build_decision() -> dict[str, Any]:
    from nyxmon.service_layer import handlers

    code = handlers._should_notify_check_result.__code__
    with _line_coverage(code) as executed:
        cases = [_run_decision(case) for case in _decision_inputs()]
    lines = {line for _, _, line in code.co_lines() if line is not None}
    missed = sorted(lines - executed - {code.co_firstlineno})
    if missed:
        raise AssertionError(
            f"decision corpus misses lines {missed} of _should_notify_check_result"
        )
    return {"cases": cases}


# ------------------------------------------------------------------- lease

LEASE_CASES: list[tuple[str, Any]] = [
    ("http", {}),
    ("http", None),
    ("http", []),
    ("http", {"timeout": 5, "retries": 2, "retry_delay": 3}),
    ("json-metrics", {"timeout": "7.5", "retries": "1.9", "retry_delay": " 2 "}),
    ("imap", {"timeout": 20, "retries": 1}),
    ("smtp", {"timeout": 30}),
    ("tcp", {}),
    ("tcp", {"connect_timeout": 2, "tls_handshake_timeout": "3", "timeout": 4.5}),
    ("tcp", {"connect_timeout": None, "tls_handshake_timeout": True, "timeout": [1]}),
    ("ping", {}),
    ("ping", {"count": 0, "timeout": 1, "interval": 0.2}),
    ("ping", {"count": 2.9, "timeout": "x", "interval": float("nan")}),
    ("ping", {"count": -4, "timeout": 10**400, "interval": "inf"}),
    ("dns", {"max_runtime_seconds": 12.1}),
    ("dns", {"max_runtime_seconds": "45"}),
    ("dns", {"max_runtime_seconds": 0, "timeout": 2}),
    ("dns", {"max_runtime_seconds": -10, "timeout": 2}),
    ("dns", {"max_runtime_seconds": 10**30}),
    ("dns", {"max_runtime_seconds": float("inf"), "timeout": float("-inf")}),
    ("dns", {"max_runtime_seconds": "nan", "timeout": "1e400"}),
    ("dns", {"max_runtime_seconds": False, "timeout": True}),
    ("http", {"timeout": 1e9, "retries": 1e9, "retry_delay": 1e9}),
    ("http", {"timeout": -0.0, "retries": -3, "retry_delay": "-1"}),
    ("http", {"timeout": "1_0", "retries": " 1 ", "retry_delay": "0x10"}),
    ("http", {"timeout": {"a": 1}}),
    ("imap", {"timeout": 0.1, "retries": 3, "retry_delay": 0.3}),
]


def build_lease() -> dict[str, Any]:
    from nyxmon.adapters.collector import estimated_check_runtime_seconds
    from nyxmon.domain.models import Check

    cases = []
    for check_type, data in LEASE_CASES:
        check = Check(
            check_id=9, service_id=1, check_type=check_type, url="u", data=data
        )
        for with_keys in (True, False):
            with _captured() as warnings:
                keys: set[tuple[int, str]] | None = set() if with_keys else None
                first = estimated_check_runtime_seconds(check, keys)
                second = estimated_check_runtime_seconds(check, keys)
            cases.append(
                {
                    "check_type": check_type,
                    "data_json": _dumps(data),
                    "with_warning_keys": with_keys,
                    "seconds": [first, second],
                    "warnings": warnings,
                }
            )
    return {"cases": cases}


# -------------------------------------------------------- incident markers


def build_incident_payloads() -> dict[str, Any]:
    from nyxmon.adapters.repositories.interface import (
        carry_delivery_markers,
        with_delivery_intent,
        without_delivery_markers,
    )

    payloads: list[dict[str, Any]] = [
        {},
        {"incident_type": "stale_processing_lease", "reclaimed_count": 2},
        {"delivery_attempt": 1, "a": 1, "delivery_pending": True, "b": [1]},
        {"delivery_pending": False, "x": None},
        {"delivery_attempt": "3"},
    ]
    cases = []
    for payload in payloads:
        for existing in payloads:
            cases.append(
                {
                    "payload_json": _dumps(payload),
                    "existing_json": _dumps(existing),
                    "carried_json": _dumps(carry_delivery_markers(payload, existing)),
                }
            )
    intents = [
        {
            "payload_json": _dumps(payload),
            "attempt": attempt,
            "with_intent_json": _dumps(with_delivery_intent(payload, attempt)),
            "without_markers_json": _dumps(without_delivery_markers(payload)),
        }
        for payload in payloads
        for attempt in (1, 7)
    ]
    return {"delivery_markers": {"carry": cases, "intent": intents}}


GENERATORS: dict[str, Callable[[], dict[str, Any]]] = {
    "decision.json": build_decision,
    "incident_payloads.json": build_incident_payloads,
    "lease.json": build_lease,
    "policy.json": build_policy,
    "site_dependency.json": build_site_dependency,
    "site_snapshot.json": build_site_snapshot,
    "suppression_rules.json": build_suppression_rules,
}
