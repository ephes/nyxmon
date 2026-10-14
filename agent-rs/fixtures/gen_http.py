"""Fixture builders of slice S5 (HTTP family), loaded by ``generate.py``.

* ``config_http.json``, ``config_json_metrics.json``: ``from_dict`` +
  ``validate`` outcomes for a data corpus (``cases``) and ``executor_cases``:
  the real Python executor run on a scripted httpx ``MockTransport`` (result,
  key order, requests, retry delays; messages derived from httpx or Python
  internals stored as ``<P2>``).
* ``httpx_url.json``: httpx URL parsing and redirect resolution.
* ``metrics_eval.json``: json-metrics path resolution, evaluation, failure
  summaries and the ``actual`` truncation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from unittest import mock


# Python set constants whose repr appears in configuration messages; the
# generator replaces their (hash-seed dependent) repr by the sorted form.
def _normalise_set_reprs(message: str) -> str:
    from nyxmon.domain import json_metrics_config

    for constant in (
        json_metrics_config.ALLOWED_OPERATORS,
        json_metrics_config.ALLOWED_SEVERITIES,
    ):
        rendered = "{" + ", ".join(repr(item) for item in sorted(constant)) + "}"
        message = message.replace(repr(constant), rendered)
    return message


# Durations and integers of PLAN.md 5.5 / test_config_numeric_contracts.py.
_NUMERIC_VALUES: list[Any] = [
    0,
    0.0,
    -0.0,
    0.25,
    1,
    1.0,
    1.9,
    100,
    101,
    200.0,
    300,
    300.0,
    300.5,
    301,
    599,
    600,
    -1,
    -0.5,
    True,
    False,
    "5",
    "junk",
    None,
    [],
    {},
    [1],
    2**63,
    10**400,
    float("inf"),
    -float("inf"),
    float("nan"),
]


def _http_config_corpus() -> list[Any]:
    corpus: list[Any] = [
        {},
        None,
        [],
        ["not", "an", "object"],
        "x",
        5,
        {"timeout": 5, "retries": 2, "retry_delay": 0},
        {"retry_status_codes": None},
        {"retry_status_codes": "502"},
        {"retry_status_codes": {}},
        {"retry_status_codes": []},
        {"retry_status_codes": [500, 502]},
        {"retry_status_codes": [502, "503"]},
        {"follow_redirects": False},
        {"follow_redirects": 0},
        {"follow_redirects": "false"},
        {"follow_redirects": None},
        {"expected_location": None},
        {"expected_location": 5},
        {"expected_location": ""},
        {"expected_location": "", "follow_redirects": False},
        {"expected_location": "/next"},
        {"expected_location": "/next", "follow_redirects": False},
        {
            "expected_location": "https://example.test/",
            "follow_redirects": False,
            "expected_status": 301,
        },
        {"expected_status": None},
        # Error precedence: from_dict checks, then validate in field order.
        {"timeout": 0, "retries": -1, "follow_redirects": "x"},
        {"timeout": 0, "retries": -1, "retry_status_codes": "x"},
        {"timeout": 0, "retries": -1},
        {"retry_delay": -1, "retries": -1},
        {"retries": -1, "retry_status_codes": [1]},
        {"retry_status_codes": [1], "expected_status": 1},
        {"expected_status": 1, "expected_location": "", "follow_redirects": False},
        {"timeout": 0, "expected_location": 5},
    ]
    for name in ("timeout", "retry_delay", "retries", "expected_status"):
        corpus.extend({name: value} for value in _NUMERIC_VALUES)
    corpus.extend({"retry_status_codes": [value]} for value in _NUMERIC_VALUES)
    return corpus


def _http_config_outcome(data: Any) -> dict[str, Any]:
    from nyxmon.domain.http_config import HttpCheckConfig

    try:
        config = HttpCheckConfig.from_dict(data)
        config.validate()
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "ok": {
            "timeout": float(config.timeout),
            "retries": config.retries,
            "retry_delay": float(config.retry_delay),
            "retry_status_codes": config.retry_status_codes,
            "follow_redirects": config.follow_redirects,
            "expected_status": config.expected_status,
            "expected_location": config.expected_location,
        }
    }


def _json_metrics_config_corpus() -> list[Any]:
    rule = {"path": "$.a", "op": "<", "value": 1, "severity": "warning"}
    base = {"url": "http://metrics.test/m", "checks": [rule]}
    corpus: list[Any] = [
        base,
        None,
        [],
        "x",
        5,
        {},
        {"url": ""},
        {"url": None, "checks": [rule]},
        {"url": 0, "checks": [rule]},
        {"url": 5, "checks": [rule]},
        {"url": ["x"], "checks": [rule]},
        {"url": True, "checks": [rule]},
        {"url": "http://metrics.test/m"},
        {"url": "http://metrics.test/m", "checks": []},
        {"url": "http://metrics.test/m", "checks": {}},
        {"url": "http://metrics.test/m", "checks": {"a": 1}},
        {"url": "http://metrics.test/m", "checks": "abc"},
        {"url": "http://metrics.test/m", "checks": 5},
        {"url": "http://metrics.test/m", "checks": 1.5},
        {"url": "http://metrics.test/m", "checks": True},
        {"url": "http://metrics.test/m", "checks": [None]},
        {"url": "http://metrics.test/m", "checks": [[rule]]},
        {"url": "http://metrics.test/m", "checks": ["x"]},
        {"url": "http://metrics.test/m", "checks": [5]},
        {"url": "http://metrics.test/m", "checks": [{}]},
        {"url": "http://metrics.test/m", "checks": [rule, {}]},
        {"url": "http://metrics.test/m", "checks": [{"op": "<"}]},
        {"url": "http://metrics.test/m", "checks": [{"op": "<", "severity": "info"}]},
        {"url": "http://metrics.test/m", "checks": [{"op": ["<"], "severity": "x"}]},
        {"url": "http://metrics.test/m", "checks": [{"op": {}, "severity": "x"}]},
        {"url": "http://metrics.test/m", "checks": [{"op": "<", "severity": ["x"]}]},
        {"url": "http://metrics.test/m", "checks": [{"op": "=", "severity": "x"}]},
        {"url": "http://metrics.test/m", "checks": [{"op": 1, "severity": "warning"}]},
        {
            "url": "http://metrics.test/m",
            "checks": [{"op": "<", "severity": "critical"}],
        },
        {"url": "http://metrics.test/m", "checks": [{**rule, "path": ""}]},
        {"url": "http://metrics.test/m", "checks": [{**rule, "path": None}]},
        {"url": "http://metrics.test/m", "checks": [{**rule, "path": 5}]},
        {"url": "http://metrics.test/m", "checks": [{**rule, "path": []}]},
        {
            "url": "http://metrics.test/m",
            "checks": [
                {"op": op, "severity": "critical", "path": "$", "value": [1]}
                for op in ("<", "<=", ">", ">=", "==", "!=")
            ],
        },
        {**base, "checks": [{"op": "<", "severity": "warning"}], "timeout": 0},
    ]
    for auth in (
        {"username": "u", "password": "p"},
        {"username": "u"},
        {"password": "p"},
        {},
        {"username": 1, "password": None},
        ["username", "password"],
        ["username"],
        [],
        "usernamepassword",
        "username",
        "",
        5,
        0,
        True,
        False,
        1.5,
        None,
    ):
        corpus.append({**base, "auth": auth})
    for value in [
        *_NUMERIC_VALUES,
        "2",
        " 3 ",
        "1_0",
        "2.5",
        "abc",
        "nan",
        "inf",
        "1e400",
        "",
    ]:
        for name in ("timeout", "retries", "retry_delay", "max_body_bytes"):
            corpus.append({**base, name: value})
    # Error precedence.
    corpus.extend(
        [
            {**base, "auth": {}, "max_body_bytes": "x", "retries": "x"},
            {**base, "max_body_bytes": "x", "retries": "x"},
            {**base, "retries": "x", "retry_delay": "x"},
            {**base, "retries": None, "retry_delay": "x"},
            {**base, "retry_delay": None, "timeout": "x"},
            {**base, "timeout": "x", "retries": -1},
            {**base, "timeout": 0, "retries": -1},
            {**base, "retries": -1, "retry_delay": -1},
            {**base, "retry_delay": -1, "max_body_bytes": 0},
            {**base, "max_body_bytes": 0, "checks": [{**rule, "path": ""}]},
        ]
    )
    return corpus


def _json_metrics_config_outcome(data: Any) -> dict[str, Any]:
    from nyxmon.domain.json_metrics_config import JsonMetricsCheckConfig

    try:
        config = JsonMetricsCheckConfig.from_dict(data)
        config.validate()
    except ValueError as exc:
        return {"error": _normalise_set_reprs(str(exc))}
    except Exception as exc:  # noqa: BLE001 - the class is the contract
        return {"exception": type(exc).__name__}
    try:
        timeout = float(config.timeout)
    except OverflowError:
        timeout = float("inf")
    return {
        "ok": {
            "url": config.url,
            "checks": [
                {"path": c.path, "op": c.op, "value": c.value, "severity": c.severity}
                for c in config.checks
            ],
            "timeout": timeout,
            "auth": config.auth,
            "retries": config.retries,
            "retry_delay": config.retry_delay,
            "max_body_bytes": config.max_body_bytes,
        }
    }


# --- executor cases: the real executors on a scripted httpx MockTransport.

_HTTP_HOST = "http://http.test"
_METRICS_HOST = "http://metrics.test"


def _respond(
    status: int, body: bytes | str = b"", headers: list[list[str]] | None = None
) -> dict[str, Any]:
    raw = body.encode() if isinstance(body, str) else body
    return {
        "respond": {"status": status, "headers": headers or [], "body_hex": raw.hex()}
    }


_TIMEOUT_STEP: dict[str, Any] = {"timeout": True}
_DISCONNECT_STEP: dict[str, Any] = {"disconnect": True}

# error_type values whose message comes from httpx or Python internals
# (PLAN.md 1.3 P2): the fixture keeps the type, not the text.
_P2_ERROR_TYPES = {"timeout", "connection_error", "request_error", "unexpected_error"}


def _run_executor_case(
    executor_cls: Any, check_type: str, url: str, data: Any, steps: list[dict[str, Any]]
) -> dict[str, Any]:
    import asyncio

    import anyio
    import httpx

    from nyxmon.domain import Check

    script = list(steps)
    requests: list[dict[str, Any]] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "path": request.url.raw_path.decode("ascii"),
                "authorization": request.headers.get("authorization"),
                "cookie": request.headers.get("cookie"),
            }
        )
        step = script.pop(0) if script else _respond(599, "script exhausted")
        if "timeout" in step:
            raise httpx.ReadTimeout("", request=request)
        if "disconnect" in step:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request
            )
        respond = step["respond"]
        return httpx.Response(
            respond["status"],
            headers=[tuple(h) for h in respond["headers"]],
            content=bytes.fromhex(respond["body_hex"]),
        )

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    async def run() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, timeout=10
        ) as client:
            executor = executor_cls(client)
            check = Check(
                check_id=1,
                service_id=1,
                name="c",
                check_type=check_type,
                url=url,
                data=data,
            )
            return await executor.execute(check)

    with mock.patch.object(anyio, "sleep", fake_sleep):
        try:
            result = asyncio.run(run())
        except Exception as exc:  # noqa: BLE001 - the runner reports the class
            outcome: dict[str, Any] = {"exception": type(exc).__name__}
        else:
            result_data = dict(result.data)
            keys: dict[str, Any] = {"keys": list(result_data)}
            if isinstance(result_data.get("failures"), list):
                keys["failure_keys"] = [list(f) for f in result_data["failures"]]
            if "duration_ms" in result_data:
                result_data["duration_ms"] = "<duration>"
            if result_data.get("error_type") in _P2_ERROR_TYPES or (
                # UnicodeDecodeError text of json.loads (pyjson: P2).
                "codec can't decode" in str(result_data.get("error_msg"))
            ):
                result_data["error_msg"] = "<P2>"
            outcome = {"status": str(result.status), "data": result_data, **keys}
    return {
        "check_type": check_type,
        "url": url,
        "data": data,
        "steps": steps,
        "outcome": outcome,
        "requests": requests,
        "delays": delays,
    }


def _http_executor_cases() -> list[dict[str, Any]]:
    from nyxmon.adapters.runner.executors.http_executor import HttpCheckExecutor

    url = f"{_HTTP_HOST}/check"
    fast = {"retry_delay": 0.5, "timeout": 0.5}
    redirect = lambda status, target: _respond(status, "moved", [["Location", target]])  # noqa: E731
    cases = [
        ({}, [_respond(200, "ok")]),
        ({"timeout": 0}, []),
        ({"retries": 2, **fast}, [_respond(200)]),
        ({"retries": 1, **fast}, [_respond(502), _respond(200)]),
        ({"retries": 2, **fast}, [_respond(502), _respond(503), _respond(504)]),
        ({"retries": 2, **fast}, [_respond(404)]),
        ({"retries": 2, **fast}, [_respond(500)]),
        ({}, [_respond(301)]),
        ({}, [_respond(302, "", [["Location", ""]])]),
        ({}, [_respond(304)]),
        ({}, [_respond(201)]),
        ({}, [_respond(204)]),
        ({"expected_status": 204}, [_respond(204)]),
        ({"expected_status": 200}, [_respond(404)]),
        (
            {"expected_status": 200, "retries": 1, **fast},
            [_respond(503), _respond(200)],
        ),
        (
            {"expected_status": 200, "retries": 1, **fast},
            [_respond(503), _respond(503)],
        ),
        ({"expected_status": 404, "retries": 1, **fast}, [_respond(500)]),
        (
            {"retry_status_codes": [404], "retries": 1, **fast},
            [_respond(404), _respond(200)],
        ),
        ({"retry_status_codes": [], "retries": 3, **fast}, [_respond(503)]),
        (
            {
                "expected_status": 301,
                "follow_redirects": False,
                "expected_location": "/next",
            },
            [redirect(301, "/next")],
        ),
        (
            {
                "expected_status": 301,
                "follow_redirects": False,
                "expected_location": "/next",
            },
            [redirect(301, "/other")],
        ),
        (
            {
                "expected_status": 301,
                "follow_redirects": False,
                "expected_location": "/next",
            },
            [_respond(301)],
        ),
        (
            {
                "expected_status": 301,
                "follow_redirects": False,
                "expected_location": "/next",
            },
            [_respond(200)],
        ),
        (
            {
                "expected_status": 301,
                "follow_redirects": False,
                "expected_location": "/next",
                "retries": 1,
                **fast,
            },
            [_respond(502), redirect(301, "/next")],
        ),
        ({"follow_redirects": False}, [redirect(302, "/next")]),
        ({"follow_redirects": False, "expected_status": 302}, [redirect(302, "/next")]),
        ({}, [redirect(302, "/next"), _respond(200)]),
        (
            {},
            [
                redirect(301, "next"),
                redirect(303, "/a?b=1"),
                redirect(307, "/c"),
                redirect(308, "/d"),
                _respond(204),
            ],
        ),
        ({}, [redirect(302, "/next"), _respond(503)]),
        ({}, [redirect(302, "/r") for _ in range(20)] + [_respond(200)]),
        ({}, [redirect(302, "/r") for _ in range(21)]),
        ({"retries": 1, **fast}, [_TIMEOUT_STEP, _respond(200)]),
        ({"retries": 1, **fast}, [_TIMEOUT_STEP, _TIMEOUT_STEP]),
        (fast, [_TIMEOUT_STEP]),
        ({"retries": 1, **fast}, [_DISCONNECT_STEP, _respond(200)]),
        (fast, [_DISCONNECT_STEP]),
        ({"expected_status": 200, **fast}, [_DISCONNECT_STEP]),
        (fast, [redirect(302, "/next"), _TIMEOUT_STEP]),
        (
            {},
            [
                _respond(
                    302,
                    "",
                    [
                        ["Location", "/deep/x"],
                        ["Set-Cookie", "sid=root; Path=/"],
                        ["Set-Cookie", "sid=deep; Path=/deep"],
                        ["Set-Cookie", "other=1; Path=/"],
                    ],
                ),
                _respond(200),
            ],
        ),
    ]
    cases_with_url = [
        (f"{_HTTP_HOST}/a/%2e%2e/b/./c/../d?q=%2e&x=a b", {}, [_respond(200)]),
        (f"{_HTTP_HOST}/a/b", {}, [redirect(302, "../%2e%2e/c;p?z#f"), _respond(200)]),
        (
            "http://user:p%40ss@http.test/start",
            {},
            [redirect(302, f"{_HTTP_HOST}/same"), _respond(200)],
        ),
        (
            "http://user:pw@http.test/start",
            {},
            [redirect(302, "http://other.test/x"), _respond(200)],
        ),
    ]
    return [
        _run_executor_case(HttpCheckExecutor, "http", url, data, steps)
        for data, steps in cases
    ] + [
        _run_executor_case(HttpCheckExecutor, "http", case_url, data, steps)
        for case_url, data, steps in cases_with_url
    ]


def _gzip(data: bytes) -> bytes:
    import gzip

    return gzip.compress(data, mtime=0)


def _gzip_with_header_crc(data: bytes, *, valid: bool) -> bytes:
    import struct
    import zlib

    header = b"\x1f\x8b\x08\x02" + b"\x00\x00\x00\x00" + b"\x00\xff"
    crc16 = zlib.crc32(header) & 0xFFFF
    if not valid:
        crc16 ^= 0xFFFF
    deflated = _zlib(data, raw=True)
    trailer = struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)
    return header + struct.pack("<H", crc16) + deflated + trailer


def _zlib(data: bytes, raw: bool = False) -> bytes:
    import zlib

    compressor = zlib.compressobj(wbits=-15 if raw else 15)
    return compressor.compress(data) + compressor.flush()


def _json_metrics_executor_cases() -> list[dict[str, Any]]:
    from nyxmon.adapters.runner.executors.json_metrics_executor import (
        JsonMetricsExecutor,
    )

    url = f"{_METRICS_HOST}/m"
    fast = {"retry_delay": 0.5, "timeout": 0.5}

    def rules(*specs: tuple[str, str, Any, str]) -> list[dict[str, Any]]:
        return [{"path": p, "op": o, "value": v, "severity": s} for p, o, v, s in specs]

    ok_rules = rules(
        ("$.queue", "<", 10, "critical"), ("$.state", "==", "ok", "warning")
    )
    body = '{"queue": 3, "state": "ok", "items": [1, 2, {"x": 5}]}'
    base = {"url": url, "checks": ok_rules, **fast}
    big = "[" + ", ".join(f'"{"v" * 20}{i}"' for i in range(30)) + "]"
    cases = [
        (base, [_respond(200, body)]),
        (
            {**base, "checks": rules(("$.queue", "<", 2, "warning"))},
            [_respond(200, body)],
        ),
        (
            {**base, "checks": rules(("$.queue", "<", 2, "critical"))},
            [_respond(200, body)],
        ),
        (
            {
                **base,
                "checks": rules(
                    ("$.queue", ">", 5, "warning"),
                    ("$.state", "!=", "ok", "critical"),
                    ("$.missing", "==", None, "critical"),
                    ("$.items[2].x", "==", 6, "warning"),
                    ("$.items.1", ">=", 2, "critical"),
                    ("items[9]", "==", 1, "warning"),
                    ("$.state", "<", 1, "critical"),
                    ("$", "==", None, "warning"),
                ),
            },
            [_respond(200, body)],
        ),
        ({**base, "checks": rules(("$", "==", [], "critical"))}, [_respond(200, big)]),
        (
            {**base, "checks": rules(("$.a", "==", None, "warning"))},
            [_respond(200, '{"a": null}')],
        ),
        (
            {**base, "checks": rules(("$.a", "!=", None, "warning"))},
            [_respond(200, '{"a": null}')],
        ),
        (
            {**base, "checks": rules(("$.a", "<", 1, "warning"))},
            [_respond(200, '{"a": NaN}')],
        ),
        (
            {**base, "checks": rules(("$.a", "<", 1, "warning"))},
            [_respond(200, '{"a": 123456789012345678901234567890}')],
        ),
        ({**base, "checks": [{**ok_rules[0], "path": None}]}, [_respond(200, body)]),
        (
            {**base, "checks": [ok_rules[0], {**ok_rules[1], "path": 5}]},
            [_respond(200, body)],
        ),
        ({**base, "checks": [{**ok_rules[0], "path": None}]}, [_respond(500)]),
        (base, [_respond(500), _respond(200, body)]),
        (base, [_respond(503), _respond(502)]),
        ({**base, "retries": 0}, [_respond(503)]),
        ({**base, "retries": 3}, [_respond(404)]),
        ({**base, "retries": "2"}, [_respond(500), _respond(500), _respond(200, body)]),
        (base, [_respond(302, "", [["Location", "/next"]]), _respond(200, body)]),
        (base, [_respond(304)]),
        (base, [_respond(200, "not json")]),
        (base, [_respond(200, "")]),
        (base, [_respond(200, '{"a": 1} x')]),
        (base, [_respond(200, b"\xef\xbb\xbf" + body.encode())]),
        (base, [_respond(200, body.encode("utf-16"))]),
        (base, [_respond(200, b"\xff\xfe\xfd")]),
        (base, [_respond(200, "[" + "1" * 4301 + "]")]),
        ({**base, "max_body_bytes": 10}, [_respond(200, body)]),
        ({**base, "max_body_bytes": len(body)}, [_respond(200, body)]),
        (
            {**base, "max_body_bytes": 10},
            [_respond(200, body, [["Content-Length", "0099"]])],
        ),
        (
            {**base, "max_body_bytes": len(_gzip(body.encode())) + 1},
            [_respond(200, _gzip(body.encode()), [["Content-Encoding", "gzip"]])],
        ),
        (base, [_respond(200, _gzip(body.encode()), [["Content-Encoding", "gzip"]])]),
        (
            base,
            [_respond(200, _zlib(body.encode()), [["Content-Encoding", "deflate"]])],
        ),
        (
            base,
            [
                _respond(
                    200,
                    _zlib(body.encode(), raw=True),
                    [["Content-Encoding", "deflate"]],
                )
            ],
        ),
        (
            base,
            [
                _respond(
                    200,
                    _gzip(_zlib(body.encode())),
                    [["Content-Encoding", "deflate, gzip"]],
                )
            ],
        ),
        (base, [_respond(200, body, [["Content-Encoding", "br"]])]),
        (base, [_respond(200, body, [["Content-Encoding", "Identity"]])]),
        (
            base,
            [_respond(200, b"\x1f\x8b\x08\x00garbage", [["Content-Encoding", "gzip"]])],
        ),
        (base, [_TIMEOUT_STEP, _respond(200, body)]),
        (base, [_TIMEOUT_STEP, _TIMEOUT_STEP]),
        (base, [_DISCONNECT_STEP, _DISCONNECT_STEP]),
        (
            {**base, "auth": {"username": "user", "password": "sécret"}},
            [_respond(200, body)],
        ),
        (
            {**base, "auth": {"username": "user", "password": "pw"}},
            [_respond(302, "", [["Location", "/next"]]), _respond(200, body)],
        ),
        ({**base, "auth": {"username": 1, "password": "pw"}}, [_respond(200, body)]),
        ({**base, "auth": {"username": "u", "password": None}}, [_respond(200, body)]),
        ({**base, "auth": ["username", "password"]}, [_respond(200, body)]),
        ({**base, "auth": "username:password"}, [_respond(200, body)]),
        ({**base, "url": 5}, [_respond(200, body)]),
        ({**base, "timeout": "5"}, []),
        # timeout=10**400 fails with OverflowError in httpcore's pool, which
        # the mock transport bypasses: covered by a Rust fake-server test.
        ({**base, "timeout": 10**300}, [_respond(200, body)]),
        (
            base,
            [
                _respond(
                    200,
                    _gzip_with_header_crc(body.encode(), valid=True),
                    [["Content-Encoding", "gzip"]],
                )
            ],
        ),
        (
            base,
            [
                _respond(
                    200,
                    _gzip_with_header_crc(body.encode(), valid=False),
                    [["Content-Encoding", "gzip"]],
                )
            ],
        ),
        ({**base, "retries": None}, []),
        ({**base, "retries": "abc"}, []),
        ({"url": url, "checks": []}, []),
    ]
    return [
        _run_executor_case(JsonMetricsExecutor, "json-metrics", url, data, steps)
        for data, steps in cases
    ]


_URL_CORPUS = [
    "http://a/%2e%2e/b",
    "http://A.Test:80/x/../y/./z?q=a b#f",
    "https://h:443",
    "https://h:443/",
    "http://h:08/",
    "http://h:+8_0/",
    "http://h: 8/",
    "http://h:x/",
    "http://h:99999/",
    "http://[::1]:8080/a",
    "http://[::1/",
    "http://[::g]/",
    "http://[fe80::1%25eth0]/",
    "http://u:p%40@h/",
    "http://u@h/",
    "http://:p@h/",
    "http://a@b@h/",
    'http://h/a b{}|^`"<>',
    "HTTP://h/\u00e9?\u00e9#\u00e9",
    "http://wersd\u00f6rfer.de/",
    "http://WERSD\u00d6RFER.de/",
    "http://\uff45xample.com/",
    "http://e\u0301.com/",
    "http://\u2603.net/",
    "http://stra\u00dfe.de/",
    "http://a\u200db.com/",
    "http://\u2167.com/",
    "http://\u0627\u0644.com/",
    "http://ex\u00e4mple..com/",
    "http://l\u00b7l.cat/",
    "http://a\u00b7b.cat/",
    "http://\u0915\u094d\u200c\u0937.in/",
    "http://a\u200cb.com/",
    "http://\u0915\u094d\u200d\u0937.in/",
    "http://\u05d0\u05d1.il/",
    "http://a\u05d0.il/",
    "http://\u05d0a.il/",
    "http://\u05d0\u05f3.il/",
    "http://a\u05f3.il/",
    "http://\u03b1\u0375\u03b2.gr/",
    "http://\u30a2\u30fb\u30a2.jp/",
    "http://a\u30fbb.jp/",
    "http://\u0661\u06f1.com/",
    "http://\u0661\u0662.com/",
    "http://\u0301a.com/",
    "http://\u00e9-.com/",
    "http://ab--\u00e9.com/",
    "http://xn--9ca.\u00e9.com/",
    "http://\u00e9.com./",
    "http://\u00e9\u3002com/",
    "http://_x.\u00e9.com/",
    "http://\u0627\u0661.com/",
    "http://\u0627a.com/",
    "http://\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9\u00e9.com/",
    "http://xn---9ca.\u00e9/",
    "http://xn--9ca-.\u00e9/",
    "http://xn--.\u00e9/",
    "http://xn--zzzzzzz.\u00e9/",
    "http://[::1%]/",
    "http://[::1%a%b]/",
    "http://[::1%25a]/",
    "http://h\\x/y",
    "http://H%41/",
    "http://h/%zz%41",
    "http://a/b;c?d",
    "http://a/b?",
    "http://a/b#",
    "http://a?x",
    "http://a#x",
    "http://1.2.3.4/",
    "http://1.2.3.256/",
    "http://01.2.3.4/",
    "http://1.2.3/",
    "//h/x",
    "http:/x",
    "http:x",
    "/rel",
    "rel/x",
    "example.com/x",
    "localhost:8080/x",
    ":x",
    "//",
    "",
    "http://h/\x7f",
    "http://h/\t",
    "ftp://h/x",
    "ftp://h:21/x",
    "ws://h:80/x",
    "http://h/a/./b/../../../c",
    "http://h/./",
    "http://h/..",
    "http://h/.../a",
    "http://h/a/.",
]

_JOIN_CORPUS = [
    ("http://h/a/b?q", "../c"),
    ("http://h/a/b", "%2e%2e/c"),
    ("http://h/a/b", "https:/x"),
    ("http://h/a/b", "http:x"),
    ("http://h:8080/a", "http:"),
    ("http://h:8080/a", "https:"),
    ("http://h/a#f", "/x"),
    ("http://h/a#f", "/x#g"),
    ("http://h/a", "//o/p"),
    ("http://h/a", "//o"),
    ("http://h/a", "?z"),
    ("http://h/a?y", "#z"),
    ("http://h/a/b/", "./"),
    ("http://h/a/b/", "."),
    ("http://h/a/b/", ".."),
    ("http://h/a/b/", "../../../.."),
    ("http://h/a", ""),
    ("http://h/a", "g;x?y#s"),
    ("http://h/a;p/b;q", "c"),
    ("http://h/a;p/b;q", ";r"),
    ("http://h/a//b//c", "d"),
    ("http://h/a//b//", "../d"),
    ("http://h", "x"),
    ("http://h/a b/", "c d"),
    ("http://h/a", "ftp://o/x"),
    ("http://h/a", "mailto:x@y"),
    ("http://h/a", "http://[::1"),
    ("http://h/a", "http://h:x/"),
    ("http://h/a", "\u00e9/x"),
    ("https://h/a", "//[::1]:8443/x"),
    ("http://u:p@h/a", "/b"),
]


def build_httpx_url() -> dict[str, Any]:
    import httpx

    parse_cases = []
    for text in _URL_CORPUS:
        try:
            url = httpx.URL(text)
        except httpx.InvalidURL:
            parse_cases.append({"url": text, "error": "InvalidURL"})
            continue
        parse_cases.append(
            {
                "url": text,
                "str": str(url),
                "scheme": url.scheme,
                "host": url.raw_host.decode("ascii"),
                "port": url.port,
                "raw_path": url.raw_path.decode("ascii"),
                "netloc": url.netloc.decode("ascii"),
                "username": url.username,
                "password": url.password,
                "is_relative": url.is_relative_url,
            }
        )
    client = httpx.Client()
    redirect_cases = []
    for base, location in _JOIN_CORPUS:
        request = client.build_request("GET", base)
        response = httpx.Response(
            302, headers=[(b"Location", location.encode("utf-8"))], request=request
        )
        try:
            target = client._redirect_url(request, response)
        except Exception as exc:  # noqa: BLE001 - the class is the contract
            redirect_cases.append(
                {
                    "base": base,
                    "location": location,
                    "error": type(exc).__name__,
                    "request_error": isinstance(exc, httpx.RequestError),
                }
            )
            continue
        redirect_cases.append(
            {
                "base": base,
                "location": location,
                "str": str(target),
                "raw_path": target.raw_path.decode("ascii"),
            }
        )
    client.close()
    return {"parse": parse_cases, "redirect": redirect_cases}


def build_config_http() -> dict[str, Any]:
    return {
        "cases": [
            {"data": data, "outcome": _http_config_outcome(data)}
            for data in _http_config_corpus()
        ],
        "executor_cases": _http_executor_cases(),
    }


def build_config_json_metrics() -> dict[str, Any]:
    return {
        "cases": [
            {"data": data, "outcome": _json_metrics_config_outcome(data)}
            for data in _json_metrics_config_corpus()
        ],
        "executor_cases": _json_metrics_executor_cases(),
    }


def build_metrics_eval() -> dict[str, Any]:
    from nyxmon.adapters.runner.executors import json_metrics_executor as jm
    from nyxmon.domain.json_metrics_config import JsonMetricsCheckConfig

    executor = jm.JsonMetricsExecutor(None)
    payload = {
        "a": {
            "b": {"c": 1},
            "list": [10, 20, [30, {"d": "deep"}]],
            "": "empty",
            "1": "one",
        },
        "x.y": 2,
        "$": "dollar",
        "list": [{"v": 1}, {"v": 2}],
        "n": None,
        "t": True,
        "f": 0.5,
        "s": "text",
        "u": "é€\U0001f600",
    }
    paths = [
        "$",
        "$.a",
        "a",
        "$.a.b.c",
        "a.b.c",
        "$.a.b.c.d",
        "$.a.list.0",
        "$.a.list[1]",
        "$.a.list[2][1].d",
        "$.a.list.2.1.d",
        "$.a.list.3",
        "$.a.list.-1",
        "$.a.list.01",
        "$.a.list.00",
        "$.a.list[x]",
        "$.a.list[]",
        "$.a.list[1",
        "$.a.list.1]",
        "$.list.1.v",
        "$.list[0].v",
        "list[0]v",
        "$..a",
        "$.a..b.c",
        ".a",
        "a.",
        "$.",
        "$.$.a",
        "$.a.$.b",
        "$$.a",
        "$.x.y",
        "x.y",
        "$.$",
        "$.a.",
        "$.a.1",
        "$.a.",
        "$.n",
        "$.n.x",
        "$.t",
        "$.s.0",
        "$.missing",
        "",
        "$a",
        "$.a.list.2.0",
        "$.a.list.99999999999999999999999",
        "$.a.list." + "0" * 4300,
        "$.a.list." + "0" * 4301,
        "$.a.list[" + "0" * 4301 + "]",
        "[0]",
        "$[0]",
        "$.list[0][",
        "$.a[b]",
        "$.u",
    ]
    resolve_cases = []
    for path in paths:
        try:
            value = executor._resolve_path(payload, path)
        except Exception as exc:  # noqa: BLE001
            resolve_cases.append({"path": path, "exception": type(exc).__name__})
            continue
        resolve_cases.append(
            {
                "path": path,
                "missing": value is jm._MISSING,
                "value": None if value is jm._MISSING else value,
            }
        )
    list_payload = [[1, 2], {"k": [3]}]
    for path in ("$", "0", "$.0.1", "$[1].k[0]", "1.k", "2", "$.0.2", "k"):
        value = executor._resolve_path(list_payload, path)
        resolve_cases.append(
            {
                "payload": list_payload,
                "path": path,
                "missing": value is jm._MISSING,
                "value": None if value is jm._MISSING else value,
            }
        )

    bounded_cases = [
        {"actual": actual, "result": executor._bounded_actual(actual)}
        for actual in (
            None,
            True,
            1,
            1.5,
            float("nan"),
            float("inf"),
            "a" * 197,
            "a" * 198,
            "a" * 199,
            "é" * 198,
            "é" * 199,
            "\U0001f600" * 198,
            "\n" * 99,
            "\n" * 100,
            '"' * 99,
            list(range(60)),
            {"k" * 100: "v" * 100},
            [[]] * 100,
            [[]] * 50,
            10**300,
            "\x00" * 40,
        )
    ]

    def evaluate(payload_doc: Any, specs: list[dict[str, Any]]) -> dict[str, Any]:
        config = JsonMetricsCheckConfig.from_dict({"url": "u", "checks": specs})
        try:
            failures = executor._evaluate(payload_doc, config)
        except Exception as exc:  # noqa: BLE001
            return {
                "payload": payload_doc,
                "checks": specs,
                "exception": type(exc).__name__,
            }
        summary = executor._build_failure_summary(failures)
        status = None
        if failures:
            status = (
                "error"
                if any(f.get("severity") == "critical" for f in failures)
                else "warning"
            )
        return {
            "payload": payload_doc,
            "checks": specs,
            "failures": failures,
            "summary": summary,
            "status": status,
        }

    ops = ["<", "<=", ">", ">=", "==", "!="]
    values: list[Any] = [
        None,
        True,
        False,
        0,
        1,
        2,
        1.0,
        1.5,
        float("nan"),
        "1",
        "a",
        "b",
        [],
        [1],
        [1, 2],
        {},
        {"a": 1},
        10**30,
    ]
    evaluate_cases = []
    for actual in values:
        for expected in values:
            evaluate_cases.append(
                evaluate(
                    {"v": actual},
                    [
                        {
                            "path": "$.v",
                            "op": op,
                            "value": expected,
                            "severity": "warning",
                        }
                        for op in ops
                    ],
                )
            )
    evaluate_cases.extend(
        [
            evaluate(
                payload,
                [
                    {"path": "$.a.b.c", "op": "==", "value": 2, "severity": "critical"},
                    {
                        "path": "$.missing",
                        "op": "!=",
                        "value": "x",
                        "severity": "warning",
                    },
                    {
                        "path": "$.s",
                        "op": "==",
                        "value": "text",
                        "severity": "critical",
                    },
                    {"path": "$.f", "op": ">", "value": 1, "severity": "critical"},
                    {"path": "$", "op": "==", "value": 1, "severity": "warning"},
                ],
            ),
            evaluate(
                payload,
                [{"path": "$.n", "op": "==", "value": None, "severity": "critical"}],
            ),
            evaluate(
                payload,
                [{"path": "$.n", "op": "!=", "value": None, "severity": "critical"}],
            ),
            evaluate(
                payload,
                [
                    {
                        "path": "$.missing",
                        "op": "==",
                        "value": None,
                        "severity": "critical",
                    }
                ],
            ),
            evaluate(
                payload,
                [
                    {"path": "$.s", "op": "==", "value": "x", "severity": "warning"},
                    {"path": None, "op": "==", "value": 1, "severity": "warning"},
                ],
            ),
            evaluate(
                payload, [{"path": 1, "op": "==", "value": 1, "severity": "warning"}]
            ),
            evaluate(
                payload,
                [
                    {
                        "path": "$.a.list." + "0" * 4301,
                        "op": "==",
                        "value": 1,
                        "severity": "warning",
                    }
                ],
            ),
            evaluate(
                "scalar",
                [{"path": "$.x", "op": "==", "value": 1, "severity": "critical"}],
            ),
            evaluate(
                [1], [{"path": "$", "op": "<", "value": [2], "severity": "critical"}]
            ),
            evaluate(
                {"v": "x" * 300},
                [{"path": "$.v", "op": "==", "value": "y", "severity": "critical"}],
            ),
            evaluate(
                {"v": {"x" * 10: "é" * 300}},
                [{"path": "v", "op": "==", "value": {}, "severity": "warning"}],
            ),
        ]
    )
    return {
        "resolve": resolve_cases,
        "bounded_actual": bounded_cases,
        "evaluate": evaluate_cases,
    }


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "config_http.json": build_config_http,
    "config_json_metrics.json": build_config_json_metrics,
    "httpx_url.json": build_httpx_url,
    "metrics_eval.json": build_metrics_eval,
}
