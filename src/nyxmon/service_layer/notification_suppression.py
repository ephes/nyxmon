from __future__ import annotations

import json
import math
import operator
import time
from typing import Any, Callable

import httpx

from ..domain.models import Check


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


# Upper bound on the suppression payload. The fetch runs on every failing
# result of a check with suppression configured, so an oversized or endless
# body must not be buffered; anything larger suppresses nothing.
MAX_SUPPRESSION_BODY_BYTES = 256 * 1024


class _Missing:
    """Sentinel for a JSON path that does not exist (distinct from ``null``)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


MISSING: Any = _Missing()


class _BodyTooLarge(Exception):
    pass


def _lookup(payload: Any, path: str) -> Any:
    """Resolve ``path`` in ``payload``; return ``MISSING`` if it does not exist."""
    if path == "$":
        return payload

    parts = [p for p in path.replace("$.", "").split(".") if p]
    current = payload
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if not 0 <= index < len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def _resolve_path(payload: Any, path: str) -> Any:
    value = _lookup(payload, path)
    return None if value is MISSING else value


def _json_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _rule_matches(payload: Any, rule: dict[str, Any]) -> bool:
    """Return True only when the rule positively matches a present value.

    A rule silences alerts, so every doubt resolves to "no match": a path
    that does not exist in the payload never matches (whatever the
    operator, so ``!=`` and ``== null`` cannot match an absent field), and
    neither does a value whose JSON type differs from the rule's value
    (so ``true <= 24`` or ``0 != "inactive"`` cannot match).
    """
    op = str(rule.get("op") or "")
    if op not in OPERATORS:
        return False
    actual = _lookup(payload, str(rule.get("path") or ""))
    if actual is MISSING:
        return False
    expected = rule.get("value")
    if _json_kind(actual) != _json_kind(expected):
        return False
    try:
        return bool(OPERATORS[op](actual, expected))
    except Exception:
        return False


def _read_capped_body(response: httpx.Response) -> bytes:
    """Read the raw body, failing on anything over MAX_SUPPRESSION_BODY_BYTES.

    A compressed response is refused rather than decompressed, so a small
    gzip body cannot expand past the cap in memory. With only the identity
    encoding left, ``iter_bytes`` yields the network chunks as received.
    """
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise _BodyTooLarge(f"compressed body ({encoding}) refused")
    declared = _int_or_none(response.headers.get("content-length"))
    if declared is not None and declared > MAX_SUPPRESSION_BODY_BYTES:
        raise _BodyTooLarge(declared)
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > MAX_SUPPRESSION_BODY_BYTES:
            raise _BodyTooLarge(len(body) + len(chunk))
        body.extend(chunk)
    return bytes(body)


MAX_SUPPRESSION_REDIRECTS = 5


def _fetch_payload(url: str, *, timeout: float, auth: httpx.BasicAuth | None) -> Any:
    """GET ``url`` and decode JSON, reading at most MAX_SUPPRESSION_BODY_BYTES.

    Redirects are followed by hand so that no redirect body is ever read:
    httpx's own ``follow_redirects`` buffers each redirect body in full.
    Credentials are sent with the first request only; ``next_request`` keeps
    the ``Authorization`` header on same-origin redirects and drops it
    otherwise, as httpx does.
    """
    with httpx.Client(
        follow_redirects=False, headers={"Accept-Encoding": "identity"}
    ) as client:
        request = client.build_request("GET", url, timeout=timeout)
        request_auth: httpx.BasicAuth | None = auth
        for _ in range(MAX_SUPPRESSION_REDIRECTS + 1):
            response = client.send(request, auth=request_auth, stream=True)
            try:
                if response.is_redirect and response.next_request is not None:
                    request = response.next_request
                    request_auth = None
                    continue
                response.raise_for_status()
                body = _read_capped_body(response)
            finally:
                response.close()
            return json.loads(body)
    raise httpx.TooManyRedirects("too many redirects", request=request)


def _build_auth(config: dict[str, Any]) -> httpx.BasicAuth | None:
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return None
    username = auth.get("username")
    password = auth.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    return httpx.BasicAuth(username, password)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_seconds(value: Any) -> float | None:
    """Return ``value`` as a finite, non-negative number of seconds, else None.

    Payload and config values are untrusted JSON. ``float()`` raises
    ``OverflowError`` on an arbitrarily large integer, and ``nan``, ``inf``
    and negative numbers compare in ways that would let an unusable value
    pass a freshness comparison. Every such value is reported as unusable so
    the caller can fail open instead of raising or suppressing.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timeout_seconds(value: Any) -> float:
    timeout = _float_or_default(value, 3.0)
    if timeout <= 0:
        return 3.0
    return min(timeout, 30.0)


def notification_suppression_details(
    check: Check, *, now_epoch: int | None = None
) -> dict[str, Any] | None:
    config = check.data.get("notification_suppression")
    if not isinstance(config, dict):
        return None

    url = str(config.get("url") or "").strip()
    if not url:
        return None

    timeout = _timeout_seconds(config.get("timeout", 3.0))
    now = now_epoch if now_epoch is not None else int(time.time())

    # Fail open on any fetch problem, including a body over the size cap.
    try:
        payload = _fetch_payload(url, timeout=timeout, auth=_build_auth(config))
    except Exception:
        return None

    # Fail open on a stale suppression payload.
    #
    # The suppression source is often the SAME endpoint whose freshness the
    # check itself asserts. If that payload freezes while a unit happens to be
    # mid-run, every later failure - including the staleness critical that is
    # supposed to report the freeze - would be suppressed indefinitely, and the
    # alert would silence exactly the condition it exists to detect.
    #
    # When freshness_path is configured, a payload that is missing the field,
    # carries a non-numeric, non-finite, negative or overflowing value, or is
    # older than freshness_max_seconds suppresses nothing. An unusable
    # freshness_max_seconds fails open the same way. Absent config, behaviour
    # is unchanged.
    freshness_path = str(config.get("freshness_path") or "").strip()
    if freshness_path:
        max_age = _finite_seconds(_int_or_none(config.get("freshness_max_seconds")))
        age = _finite_seconds(_resolve_path(payload, freshness_path))
        if age is None or max_age is None or max_age <= 0 or age > max_age:
            return None

    active_statuses = config.get("active_statuses", ["running"])
    if not isinstance(active_statuses, list):
        active_statuses = ["running"]
    status_path = str(config.get("status_path") or "$.last_status")
    status = _resolve_path(payload, status_path)
    if isinstance(status, str) and status in active_statuses:
        return {
            "reason": str(config.get("reason") or "maintenance_active"),
            "source_url": url,
            "source_status": status,
        }

    active_if = config.get("active_if", [])
    if not isinstance(active_if, list):
        active_if = []
    for rule in active_if:
        if isinstance(rule, dict) and _rule_matches(payload, rule):
            return {
                "reason": str(config.get("reason") or "maintenance_active"),
                "source_url": url,
                "source_status": status,
                "matched_rule": {
                    "path": rule.get("path"),
                    "op": rule.get("op"),
                    "value": rule.get("value"),
                },
            }

    active_for_seconds = _int_or_none(config.get("active_for_seconds"))
    finished_epoch_path = str(
        config.get("finished_epoch_path") or "$.last_run_finished_epoch"
    )
    finished_epoch = _int_or_none(_resolve_path(payload, finished_epoch_path))
    if (
        active_for_seconds is not None
        and active_for_seconds > 0
        and finished_epoch is not None
        and 0 <= now - finished_epoch <= active_for_seconds
    ):
        return {
            "reason": str(config.get("reason") or "maintenance_recently_finished"),
            "source_url": url,
            "source_status": status,
            "finished_epoch": finished_epoch,
            "active_for_seconds": active_for_seconds,
        }

    return None
