"""Fixture builders of slice S10 (collector reliability), loaded by ``generate.py``.

* ``collector_incidents.json``: ``AsyncCheckCollector._merge_stale_batch_payload``,
  ``_stale_batch_message`` and ``_paused_batch_message`` on stored payloads of
  every JSON type (``build_incident_notification`` is checked by the S11
  tests of ``nyxmon_agent::incident_notification``).

Payloads are stored as JSON text (``*_json``) because key order matters and
the fixture file itself is written with sorted keys. An exception that the
collector does not catch is recorded as ``error`` (class and message).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from unittest import mock

NOW = 1_800_000_000
OPENED_AT = NOW - 600

#: Stored payloads, as the store decodes them (``json.loads`` of the column).
PAYLOADS: list[str] = [
    "{}",
    '{"incident_type": "stale_processing_lease", "reclaimed_count": 3, '
    '"iteration_count": 2, "check_ids": [1, 2, 3], "last_seen_at": 1799999700}',
    '{"reclaimed_count": 3, "iteration_count": 2, "check_ids": [1, 2, 3], '
    '"last_seen_at": 1799999700, "delivery_pending": true, "delivery_attempt": 1}',
    '{"reclaimed_count": 25, "iteration_count": 4, '
    '"check_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, '
    "19, 20]}",
    '{"reclaimed_count": 30, "check_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, '
    "13, 14, 15, 16, 17, 18, 19, 20, 21, 22]}",
    '{"reclaimed_count": "4", "iteration_count": " 2 ", "last_seen_at": "1799999900"}',
    '{"reclaimed_count": 2.9, "iteration_count": true, "last_seen_at": 1799999900.5}',
    '{"reclaimed_count": "x", "iteration_count": 1}',
    '{"reclaimed_count": 1, "iteration_count": "1.5"}',
    '{"reclaimed_count": null, "last_seen_at": null}',
    '{"reclaimed_count": [1], "iteration_count": {}, "last_seen_at": []}',
    '{"reclaimed_count": NaN, "last_seen_at": NaN}',
    '{"reclaimed_count": Infinity}',
    '{"reclaimed_count": 1, "iteration_count": -Infinity}',
    '{"last_seen_at": Infinity}',
    '{"reclaimed_count": true, "check_ids": []}',
    '{"reclaimed_count": 123456789012345678901234567890, "iteration_count": 1}',
    '{"check_ids": [true, 1, 1.0, "2", null, 2, [3], 99999999999999999999999, 2]}',
    '{"check_ids": "12"}',
    '{"check_ids": {"7": 1, "8": 2}}',
    '{"check_ids": null}',
    '{"check_ids": 5}',
    '{"check_ids": 1.5}',
    '{"check_ids": false}',
    '{"check_ids": ["\\u00e9", "", "x y"]}',
    '{"check_ids": [""]}',
    '{"last_seen_at": 1700000000}',
    '{"batch_size": 3, "check_ids": [4, 5, 6]}',
    '{"batch_size": "3", "check_ids": []}',
    '{"batch_size": 1.0, "check_ids": [1.5, null, false, {"a": [1]}]}',
    '{"batch_size": null}',
    '{"batch_size": [1, "a"], "check_ids": "abc"}',
]

RECOVERED: list[list[int]] = [[], [1], [3, 4, 4, 5], list(range(30, 55))]


def _outcome(func: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = func()
    except Exception as exc:  # noqa: BLE001 - every escaping exception is data
        return {"error": {"class": type(exc).__name__, "message": str(exc)}}
    return {"error": None, "value": value}


def _incident(payload_json: str, opened_at: int = OPENED_AT) -> Any:
    from nyxmon.adapters.repositories.interface import CollectorIncident

    return CollectorIncident(
        incident_key="collector:stale_processing_lease",
        opened_at=opened_at,
        last_alert_at=opened_at,
        alert_count=1,
        payload=json.loads(payload_json),
    )


def _check(check_id: int) -> Any:
    from nyxmon.domain import Check

    return Check(
        check_id=check_id,
        service_id=1,
        check_type="http",
        url="https://example.test/",
        data={},
    )


def build_collector_incidents() -> dict[str, Any]:
    from nyxmon.adapters.collector import AsyncCheckCollector

    merges = []
    for payload_json in [None, *PAYLOADS]:
        for recovered in RECOVERED:
            existing = None if payload_json is None else _incident(payload_json)
            outcome = _outcome(
                lambda: json.dumps(
                    AsyncCheckCollector._merge_stale_batch_payload(
                        existing, [_check(i) for i in recovered], NOW
                    )
                )
            )
            merges.append(
                {"existing_json": payload_json, "recovered": recovered, **outcome}
            )

    messages = []
    for payload_json in PAYLOADS:
        for opened_at in (OPENED_AT, NOW + 50):
            incident = _incident(payload_json, opened_at)
            stale = _outcome(lambda: AsyncCheckCollector._stale_batch_message(incident))
            with mock.patch(
                "nyxmon.adapters.collector.current_epoch", lambda: float(NOW)
            ):
                paused = _outcome(
                    lambda: AsyncCheckCollector._paused_batch_message(
                        None,  # type: ignore[arg-type]
                        incident,
                    )
                )
            messages.append(
                {
                    "payload_json": payload_json,
                    "opened_at": opened_at,
                    "stale": stale,
                    "paused": paused,
                }
            )

    return {"merge": merges, "messages": messages}


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "collector_incidents.json": build_collector_incidents,
}
