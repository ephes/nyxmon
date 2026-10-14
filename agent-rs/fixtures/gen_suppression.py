"""Fixture builder of slice S5b (maintenance-suppression fetch), loaded by
``generate.py``.

``suppression_fetch.json``: ``notification_suppression._fetch_payload`` (with
``_timeout_seconds`` and ``_build_auth`` of the config, as
``notification_suppression_details`` calls it) on a scripted httpx
``MockTransport``: the decoded payload, or ``failed`` when the fetch fails
(``notification_suppression_details`` fails open on every exception), and
the requests the transport received.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Mapping
from typing import Any
from unittest import mock

_HOST = "http://maint.test"
_OTHER = "http://other.test"


def _respond(
    status: int,
    body: bytes | str = b"",
    headers: list[list[str]] | None = None,
    *,
    stream: bool = False,
) -> dict[str, Any]:
    raw = body.encode() if isinstance(body, str) else body
    return {
        "respond": {
            "status": status,
            "headers": headers or [],
            "body_hex": raw.hex(),
            "stream": stream,
        }
    }


def _json(
    payload: Any = None, headers: list[list[str]] | None = None
) -> dict[str, Any]:
    body = json.dumps({"last_status": "running"} if payload is None else payload)
    return _respond(200, body, [["Content-Type", "application/json"], *(headers or [])])


def _redirect(
    status: int, location: str, headers: list[list[str]] | None = None
) -> dict[str, Any]:
    return _respond(status, "moved", [["Location", location], *(headers or [])])


def _padded(pad: int, tail: str = "", *, stream: bool = False) -> dict[str, Any]:
    """A JSON body ``{"last_status": "running", "pad": "xx..."}`` + ``tail``
    described by its padding length, so the fixture stays small."""
    step = _respond(200, b"", stream=stream)
    step["respond"]["pad"] = [pad, tail]
    return step


def _body(respond: Mapping[str, Any]) -> bytes:
    if "pad" in respond:
        pad, tail = respond["pad"]
        return (
            '{"last_status": "running", "pad": "' + "x" * pad + '"}' + tail
        ).encode()
    return bytes.fromhex(respond["body_hex"])


_TIMEOUT_STEP: dict[str, Any] = {"timeout": True}
_CAP = 256 * 1024


def _scenarios() -> list[dict[str, Any]]:
    url = f"{_HOST}/maintenance"
    auth = {"username": "nyxmon", "password": "s3cret"}
    exact = _CAP - len('{"last_status": "running", "pad": ""}')

    def case(
        steps: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
        target: str = url,
    ) -> dict[str, Any]:
        return {"config": {"url": target, **(config or {})}, "steps": steps}

    return [
        case([_json()]),
        case([_json()], {"auth": auth}),
        case([_json()], {"auth": {"username": "u"}}),
        case([_json()], {"auth": {"username": "u", "password": 5}}),
        case([_json()], {"auth": ["u", "p"]}),
        case([_json()], target="http://user:pw@maint.test/maintenance"),
        case([_json()], {"auth": auth}, target="http://user:pw@maint.test/maintenance"),
        case([_json({"a": [1, 2.5, None, True], "b": "\u00fc"})]),
        case([_respond(200, "\ufeff" + json.dumps({"bom": 1}))]),
        case([_respond(200, json.dumps({"utf16": 1}).encode("utf-16"))]),
        case([_respond(200, "not json")]),
        case([_respond(200, "")]),
        case([_respond(200, "NaN")]),
        case([_respond(200, "null")]),
        case([_respond(204)]),
        case([_respond(404, json.dumps({"last_status": "running"}))]),
        case([_respond(500)]),
        case([_respond(302, "moved")]),
        case([_respond(304)]),
        case([_redirect(302, "/final"), _json()], {"auth": auth}),
        case([_redirect(301, "/final"), _json()], {"auth": auth}),
        case([_redirect(303, "/final?x=1"), _json()], {"auth": auth}),
        case([_redirect(307, "final"), _json()], {"auth": auth}),
        case([_redirect(308, f"{_HOST}/final"), _json()], {"auth": auth}),
        case([_redirect(302, f"{_OTHER}/status"), _json()], {"auth": auth}),
        case(
            [
                _redirect(302, f"{_OTHER}/status"),
                _redirect(302, f"{_HOST}/back"),
                _json(),
            ],
            {"auth": auth},
        ),
        case(
            [_redirect(302, "/final", [["Set-Cookie", "session=abc; Path=/"]]), _json()]
        ),
        case(
            [
                _redirect(302, "/m", [["Set-Cookie", "a=1"]]),
                _redirect(302, "/m2"),
                _json(),
            ]
        ),
        case([_redirect(302, "/loop")] * 7),
        case([_redirect(302, f"/hop{i}") for i in range(5)] + [_json()]),
        case([_redirect(302, f"/hop{i}") for i in range(6)] + [_json()]),
        case([_redirect(302, "http://[::1")]),
        case([_redirect(302, "/final"), _respond(404)]),
        case([_json(headers=[["Content-Encoding", "gzip"]])]),
        case(
            [
                _respond(
                    200,
                    gzip.compress(b'{"last_status": "running"}', mtime=0),
                    [["Content-Encoding", "gzip"]],
                )
            ]
        ),
        case([_json(headers=[["Content-Encoding", "identity"]])]),
        case([_json(headers=[["Content-Encoding", " Identity "]])]),
        case([_json(headers=[["Content-Encoding", "identity, gzip"]])]),
        case([_json(headers=[["Content-Encoding", "br"]])]),
        case([_padded(_CAP)]),
        case([_padded(_CAP, stream=True)]),
        case([_padded(exact)]),
        case([_padded(exact, stream=True)]),
        case([_padded(exact, " ", stream=True)]),
        case([_TIMEOUT_STEP], {"timeout": 0.5}),
        case([_redirect(302, "/slow"), _TIMEOUT_STEP], {"timeout": 0.5}),
    ]


def _run_case(spec: Mapping[str, Any]) -> dict[str, Any]:
    import httpx

    from nyxmon.service_layer import notification_suppression as ns

    script = list(spec["steps"])
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "host": request.url.host,
                "path": request.url.raw_path.decode("ascii"),
                "authorization": request.headers.get("authorization"),
                "accept_encoding": request.headers.get("accept-encoding"),
                "cookie": request.headers.get("cookie"),
            }
        )
        step = script.pop(0) if script else _respond(599, "script exhausted")
        if "timeout" in step:
            raise httpx.ReadTimeout("", request=request)
        respond = step["respond"]
        body = _body(respond)
        headers = [tuple(h) for h in respond["headers"]]
        if respond["stream"]:

            def chunks() -> Any:
                for start in range(0, len(body), 16_384):
                    yield body[start : start + 16_384]

            return httpx.Response(respond["status"], headers=headers, content=chunks())
        return httpx.Response(respond["status"], headers=headers, content=body)

    real_client = httpx.Client

    def factory(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    config = spec["config"]
    with mock.patch.object(ns.httpx, "Client", factory):
        try:
            payload = ns._fetch_payload(
                str(config["url"]).strip(),
                timeout=ns._timeout_seconds(config.get("timeout", 3.0)),
                auth=ns._build_auth(config),
            )
        except Exception:  # noqa: BLE001 - details() fails open on everything
            outcome: dict[str, Any] = {"failed": True}
        else:
            if isinstance(payload, dict) and isinstance(payload.get("pad"), str):
                payload = {**payload, "pad": len(payload["pad"])}
            outcome = {"payload": payload}
    return {**spec, "outcome": outcome, "requests": requests}


def build_suppression_fetch() -> dict[str, Any]:
    return {"cases": [_run_case(spec) for spec in _scenarios()]}


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "suppression_fetch.json": build_suppression_fetch,
}
