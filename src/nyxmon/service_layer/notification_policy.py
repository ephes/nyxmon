"""Resolution of per-check and global notification policy.

Two knobs make up a policy:

``consecutive_failures``
    How many consecutive non-OK samples must be seen before the *first*
    notification of an incident is sent.

``reminder_seconds``
    How much wall-clock time must elapse between reminders while an incident
    stays open. This is deliberately *elapsed time*, not a sample count: a
    sample count means something different for a five-minute check than for an
    hourly one.

Global defaults come from the environment. Per-check overrides live in
``HealthCheck.data["notification_policy"]`` and are validated in the same
fail-safe style as ``notification_suppression``: anything malformed is warned
about once and falls back to the global default, never raising into the hot
path and never accidentally lowering a threshold.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ..domain.models import ResultStatus

logger = logging.getLogger(__name__)

# Two consecutive failures before the first page: a single scrape is far too
# often a transient blip, and first-sample paging was the direct cause of the
# 2026-09-01 alert storm.
DEFAULT_NOTIFY_CONSECUTIVE_FAILURES = 2

# Six hours between reminders for an open error incident.
DEFAULT_NOTIFY_REPEAT_INTERVAL_SECONDS = 6 * 3600

# Warnings are, by evidence, dominated by standing conditions that are
# acknowledged but not yet actionable (pending reboot, boundary-value disk
# usage, an external mail relay dropping probes). They stay visible, but remind
# daily rather than every few hours.
DEFAULT_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS = 24 * 3600

MIN_REMINDER_SECONDS = 60
MAX_REMINDER_SECONDS = 30 * 24 * 3600
MAX_CONSECUTIVE_FAILURES = 100

POLICY_DATA_KEY = "notification_policy"

ENV_CONSECUTIVE_FAILURES = "NYXMON_NOTIFY_CONSECUTIVE_FAILURES"
ENV_REPEAT_INTERVAL_SECONDS = "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS"
ENV_WARNING_REPEAT_INTERVAL_SECONDS = "NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS"
ENV_DEPRECATED_REPEAT_FAILURES = "NYXMON_NOTIFY_REPEAT_FAILURES"

# Warn at most once per (check_id, field) so a single malformed check cannot
# flood the log on every scrape.
_warned_policy_fields: set[tuple[int, str]] = set()
_warned_deprecated_env: set[str] = set()


def reset_policy_warning_state() -> None:
    """Forget which warnings have already been emitted (tests only)."""
    _warned_policy_fields.clear()
    _warned_deprecated_env.clear()


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    """Resolved alert policy for one check and one result severity."""

    consecutive_failures: int
    reminder_seconds: int


@lru_cache(maxsize=None)
def _bounded_env_value(
    value: str, default: int, env_name: str, low: int, high: int
) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("%s is invalid; using default %s", env_name, default)
        return default
    if parsed < low or parsed > high:
        logger.warning(
            "%s must be between %s and %s; using default %s",
            env_name,
            low,
            high,
            default,
        )
        return default
    return parsed


def _warn_deprecated_repeat_failures(value: str) -> None:
    if value in _warned_deprecated_env:
        return
    _warned_deprecated_env.add(value)
    logger.warning(
        "%s=%s is deprecated and is IGNORED: reminder cadence is now elapsed-time "
        "based. A sample count cannot be translated to a duration because it means "
        "a different interval for every check. Set %s (and optionally %s) instead.",
        ENV_DEPRECATED_REPEAT_FAILURES,
        value,
        ENV_REPEAT_INTERVAL_SECONDS,
        ENV_WARNING_REPEAT_INTERVAL_SECONDS,
    )


def _check_deprecated_env() -> None:
    value = os.environ.get(ENV_DEPRECATED_REPEAT_FAILURES, "").strip()
    if value:
        _warn_deprecated_repeat_failures(value)


def global_consecutive_failures() -> int:
    return _bounded_env_value(
        os.environ.get(ENV_CONSECUTIVE_FAILURES, "").strip(),
        DEFAULT_NOTIFY_CONSECUTIVE_FAILURES,
        ENV_CONSECUTIVE_FAILURES,
        1,
        MAX_CONSECUTIVE_FAILURES,
    )


def global_reminder_seconds(status: str) -> int:
    _check_deprecated_env()
    if status == ResultStatus.WARNING:
        return _bounded_env_value(
            os.environ.get(ENV_WARNING_REPEAT_INTERVAL_SECONDS, "").strip(),
            DEFAULT_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS,
            ENV_WARNING_REPEAT_INTERVAL_SECONDS,
            MIN_REMINDER_SECONDS,
            MAX_REMINDER_SECONDS,
        )
    return _bounded_env_value(
        os.environ.get(ENV_REPEAT_INTERVAL_SECONDS, "").strip(),
        DEFAULT_NOTIFY_REPEAT_INTERVAL_SECONDS,
        ENV_REPEAT_INTERVAL_SECONDS,
        MIN_REMINDER_SECONDS,
        MAX_REMINDER_SECONDS,
    )


def _bounded_int(
    config: dict[str, Any],
    key: str,
    *,
    check_id: int,
    low: int,
    high: int,
) -> int | None:
    if key not in config:
        return None
    raw = config[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        _warn_once(check_id, key, raw, "must be an integer")
        return None
    if raw < low or raw > high:
        _warn_once(check_id, key, raw, f"must be between {low} and {high}")
        return None
    return raw


def _warn_once(check_id: int, key: str, raw: Any, reason: str) -> None:
    marker = (check_id, key)
    if marker in _warned_policy_fields:
        return
    _warned_policy_fields.add(marker)
    logger.warning(
        "notification_policy.%s for check_id=%s is invalid (%s, got %r); "
        "using the global default",
        key,
        check_id,
        reason,
        raw,
    )


def _first_not_none(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


def resolve_notification_policy(check: Any, status: str) -> NotificationPolicy:
    """Resolve the effective policy for ``check`` at result severity ``status``.

    Never raises. Malformed per-check configuration is warned about once and
    ignored in favour of the global default.
    """
    default_threshold = global_consecutive_failures()
    default_reminder = global_reminder_seconds(status)

    data = getattr(check, "data", None)
    if not isinstance(data, dict):
        return NotificationPolicy(default_threshold, default_reminder)
    config = data.get(POLICY_DATA_KEY)
    if config is None:
        return NotificationPolicy(default_threshold, default_reminder)

    check_id = int(getattr(check, "check_id", 0) or 0)
    if not isinstance(config, dict):
        _warn_once(check_id, POLICY_DATA_KEY, config, "must be an object")
        return NotificationPolicy(default_threshold, default_reminder)

    is_warning = status == ResultStatus.WARNING
    threshold = _first_not_none(
        (
            _bounded_int(
                config,
                "warning_consecutive_failures",
                check_id=check_id,
                low=1,
                high=MAX_CONSECUTIVE_FAILURES,
            )
            if is_warning
            else None
        ),
        _bounded_int(
            config,
            "consecutive_failures",
            check_id=check_id,
            low=1,
            high=MAX_CONSECUTIVE_FAILURES,
        ),
        default_threshold,
    )
    reminder = _first_not_none(
        (
            _bounded_int(
                config,
                "warning_reminder_seconds",
                check_id=check_id,
                low=MIN_REMINDER_SECONDS,
                high=MAX_REMINDER_SECONDS,
            )
            if is_warning
            else None
        ),
        _bounded_int(
            config,
            "reminder_seconds",
            check_id=check_id,
            low=MIN_REMINDER_SECONDS,
            high=MAX_REMINDER_SECONDS,
        ),
        default_reminder,
    )
    assert threshold is not None and reminder is not None
    return NotificationPolicy(threshold, reminder)
