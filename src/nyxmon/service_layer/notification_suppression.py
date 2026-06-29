from __future__ import annotations

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


def _resolve_path(payload: Any, path: str) -> Any:
    if path == "$":
        return payload

    parts = [p for p in path.replace("$.", "").split(".") if p]
    current = payload
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def _rule_matches(payload: Any, rule: dict[str, Any]) -> bool:
    op = str(rule.get("op") or "")
    if op not in OPERATORS:
        return False
    actual = _resolve_path(payload, str(rule.get("path") or ""))
    expected = rule.get("value")
    try:
        return bool(OPERATORS[op](actual, expected))
    except Exception:
        return False


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
    except (TypeError, ValueError):
        return None


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

    try:
        with httpx.Client(follow_redirects=True) as client:
            response = client.get(url, timeout=timeout, auth=_build_auth(config))
        response.raise_for_status()
        payload = response.json()
    except Exception:
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
