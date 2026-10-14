"""Fixture builders of slice S4 (site observer core), loaded by ``generate.py``.

``site_replay/*.json`` (PLAN.md section 7.2): the real
``SiteConnectivityObserver`` driven by a scripted probe runner, an injected
clock, an in-memory incident store and scheduler with injectable failures,
and a recording notifier. Every step records, in call order, the store,
scheduler and notifier calls and the log lines of the observer (``events``),
the probed targets, the published snapshot, and the exception class a round
raised. Payloads are recorded as ``json.dumps`` text, so the Rust side
compares the stored bytes.

Step inputs:

* ``{"op": "round", "at": t, "set": {...}}`` runs ``run_once`` at ``t``;
* ``{"op": "respawn", "at": t, "set": {...}}`` builds a fresh observer over
  the same store and awaits ``restore`` (a process restart);
* ``{"op": "restore", "at": t}`` awaits ``restore`` on the current observer;
* ``{"op": "seed", "payload_json": ...}`` replaces the row's payload directly
  (no event); ``{"op": "drop_row"}`` deletes the row.

``set`` changes the persistent fake state before the step: ``down_paths``,
``down_targets``, ``raise_targets`` (probe raises), ``fail_reads``,
``fail_writes``, ``deliver`` (``delivered``/``failed``/``unknown``/``raise``),
``held`` (held check rows), ``in_flight``, ``held_at_close`` (``None`` means
``len(held)``), ``fail_in_flight``, ``fail_held``, ``fail_reschedule``,
``fail_count_held``. ``fail_write_calls`` (indices of the step's write calls
that fail) and ``deliver_calls`` (outcomes of the step's first sends) apply to
that step only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

T0 = 1_700_000_000

IPV4 = [["1.1.1.1", 443], ["8.8.8.8", 443]]
IPV6 = [["2606:4700:4700::1111", 443]]
DNS = ["cloudflare.com"]

INTERNET = json.dumps({"site_dependency": "internet"})
IPV4_ONLY = json.dumps({"site_dependency": {"requires": ["ipv4"]}})
IPV6_ONLY = json.dumps({"site_dependency": {"requires": ["ipv6"]}})


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "mode": "enforce",
        "probe_interval": 60,
        "probe_timeout": 3,
        "ipv4": IPV4,
        "ipv6": IPV6,
        "dns": DNS,
        "down_after_failures": 2,
        "recovery_grace_seconds": 900,
        "max_hold_seconds": 10800,
        "incident_notify_after_seconds": 900,
        "incident_reminder_seconds": 21600,
    }
    config.update(overrides)
    return config


def _build_config(spec: dict[str, Any]) -> Any:
    from nyxmon.adapters.site_connectivity import (
        ProbeTarget,
        SiteConnectivityConfig,
        SiteMode,
    )

    def tcp(path: str) -> tuple[Any, ...]:
        return tuple(
            ProbeTarget(path=path, kind="tcp", host=host, port=port)
            for host, port in spec[path]
        )

    return SiteConnectivityConfig(
        mode=SiteMode(spec["mode"]),
        probe_interval=spec["probe_interval"],
        probe_timeout=spec["probe_timeout"],
        ipv4_targets=tcp("ipv4"),
        ipv6_targets=tcp("ipv6"),
        dns_names=tuple(
            ProbeTarget(path="dns", kind="dns", host=name) for name in spec["dns"]
        ),
        down_after_failures=spec["down_after_failures"],
        recovery_grace_seconds=spec["recovery_grace_seconds"],
        max_hold_seconds=spec["max_hold_seconds"],
        incident_notify_after_seconds=spec["incident_notify_after_seconds"],
        incident_reminder_seconds=spec["incident_reminder_seconds"],
    )


# ------------------------------------------------------------------ fakes


class _World:
    """Fake store, scheduler, probe runner and notifier sharing one event log."""

    def __init__(self) -> None:
        self.now = T0
        self.events: list[list[Any]] = []
        self.probed: list[str] = []
        self.row: dict[str, Any] | None = None
        self.state: dict[str, Any] = {
            "down_paths": [],
            "down_targets": [],
            "raise_targets": [],
            "fail_reads": False,
            "fail_writes": False,
            "deliver": "delivered",
            "held": [],
            "in_flight": 0,
            "held_at_close": None,
            "fail_in_flight": False,
            "fail_held": False,
            "fail_reschedule": False,
            "fail_count_held": False,
        }
        self.fail_write_calls: list[int] = []
        self.deliver_calls: list[str] = []
        self.write_calls = 0

    def begin_step(self, settings: dict[str, Any]) -> None:
        settings = dict(settings)
        self.fail_write_calls = list(settings.pop("fail_write_calls", []))
        self.deliver_calls = list(settings.pop("deliver_calls", []))
        unknown = set(settings) - set(self.state)
        assert not unknown, unknown
        self.state.update(settings)
        self.events = []
        self.probed = []
        self.write_calls = 0

    # store -----------------------------------------------------------
    def _incident(self) -> Any:
        from nyxmon.adapters.repositories.interface import CollectorIncident

        assert self.row is not None
        return CollectorIncident(
            incident_key="site:connectivity",
            opened_at=T0,
            last_alert_at=0,
            alert_count=0,
            payload=json.loads(json.dumps(self.row)),
        )

    def get_collector_incident(self, incident_key: str) -> Any:
        if self.state["fail_reads"]:
            self.events.append(["store", "get", "error"])
            raise RuntimeError("store is unavailable")
        self.events.append(["store", "get", "ok" if self.row is not None else "none"])
        return None if self.row is None else self._incident()

    def _write_fails(self) -> bool:
        index = self.write_calls
        self.write_calls += 1
        return bool(self.state["fail_writes"]) or index in self.fail_write_calls

    def set_collector_incident_payload(
        self, incident_key: str, payload: dict[str, Any]
    ) -> Any:
        text = json.dumps(payload)
        if self._write_fails():
            self.events.append(["store", "set", "error", text])
            raise RuntimeError("store is unavailable")
        if self.row is None:
            self.events.append(["store", "set", "none", text])
            return None
        self.events.append(["store", "set", "ok", text])
        self.row = json.loads(text)
        return self._incident()

    def open_collector_incident(
        self, incident_key: str, *, now: int, payload: dict[str, Any]
    ) -> Any:
        text = json.dumps(payload)
        if self._write_fails():
            self.events.append(["store", "open", "error", text, now])
            raise RuntimeError("store is unavailable")
        self.events.append(["store", "open", "ok", text, now])
        self.row = json.loads(text)
        return self._incident()

    # scheduler -------------------------------------------------------
    async def list_held_checks_async(self) -> list[Any]:
        from types import SimpleNamespace

        self.events.append(["sched", "held"])
        if self.state["fail_held"]:
            raise RuntimeError("database is busy")
        return [
            SimpleNamespace(**{**row, "data": json.loads(row["data_json"])})
            for row in self.state["held"]
        ]

    async def reschedule_checks_async(self, check_ids: list[int], *, run_at: int) -> int:
        self.events.append(["sched", "reschedule", list(check_ids), run_at])
        if self.state["fail_reschedule"]:
            raise RuntimeError("database is busy")
        return len(check_ids)

    async def count_held_checks_async(self) -> int:
        self.events.append(["sched", "count_held"])
        if self.state["fail_count_held"]:
            raise RuntimeError("database is busy")
        held_at_close = self.state["held_at_close"]
        return len(self.state["held"]) if held_at_close is None else held_at_close

    def count_held_checks(self) -> int:
        raise AssertionError("the observer must use the async counter")

    async def count_processing_claims_before_async(self, epoch: int) -> int:
        self.events.append(["sched", "in_flight", epoch])
        if self.state["fail_in_flight"]:
            raise RuntimeError("database is busy")
        return int(self.state["in_flight"])

    # probes ----------------------------------------------------------
    async def probe(self, target: Any) -> bool:
        name = str(target)
        self.probed.append(name)
        if name in self.state["raise_targets"]:
            raise RuntimeError("probe exploded")
        if name in self.state["down_targets"]:
            return False
        return target.path not in self.state["down_paths"]

    # notifier --------------------------------------------------------
    def notify(
        self,
        incident_key: str,
        name: str,
        error_type: str,
        error_msg: str,
        status: str,
        opsgate_ticket: bool,
    ) -> bool | None:
        self.events.append(
            ["send", incident_key, name, error_type, error_msg, status, opsgate_ticket]
        )
        outcome = (
            self.deliver_calls.pop(0) if self.deliver_calls else self.state["deliver"]
        )
        if outcome == "raise":
            raise RuntimeError("telegram exploded")
        return {"delivered": True, "failed": False, "unknown": None}[outcome]


@contextmanager
def _logs(world: _World) -> Iterator[None]:
    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            world.events.append(
                ["log", record.levelname, record.name, record.getMessage()]
            )

    handler = _Handler(level=logging.INFO)
    root = logging.getLogger("nyxmon")
    previous, propagate = root.level, root.propagate
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
        root.propagate = propagate


def _snapshot(observer: Any) -> list[Any]:
    snapshot = observer.snapshot()
    return [
        snapshot.observed_at,
        str(snapshot.mode),
        snapshot.incident_id,
        {
            name: [
                path.state,
                path.down_since,
                path.recovered_at,
                path.release_at,
                path.last_release_at,
            ]
            for name, path in snapshot.paths.items()
        },
    ]


def _replay(scenario: dict[str, Any]) -> dict[str, Any]:
    import anyio

    from nyxmon.adapters import site_connectivity
    from nyxmon.adapters.site_connectivity import SiteConnectivityObserver
    from nyxmon.service_layer import site_dependency

    site_connectivity.reset_site_config_warning_state()
    site_dependency.reset_dependency_warning_state()
    world = _World()
    config = _build_config(scenario["config"])

    def build() -> Any:
        return SiteConnectivityObserver(
            config,
            incident_store=world,
            scheduler=world,
            probe_runner=world,
            clock=lambda: world.now,
            notifier=world.notify,
        )

    async def main() -> list[dict[str, Any]]:
        observer = build()
        results: list[dict[str, Any]] = []
        for step in scenario["steps"]:
            op = step["op"]
            if op == "seed":
                world.row = json.loads(step["payload_json"])
                continue
            if op == "drop_row":
                world.row = None
                continue
            world.begin_step(step.get("set", {}))
            world.now = step["at"]
            raised = None
            with _logs(world):
                try:
                    if op == "respawn":
                        observer = build()
                        await observer.restore()
                    elif op == "restore":
                        await observer.restore()
                    else:
                        assert op == "round", op
                        await observer.run_once()
                except Exception as exc:  # noqa: BLE001 - recorded for parity
                    raised = type(exc).__name__
            results.append(
                {
                    "events": world.events,
                    "probed": world.probed,
                    "snapshot": _snapshot(observer),
                    "raised": raised,
                }
            )
        return results

    return {**scenario, "results": anyio.run(main, backend="asyncio")}


# -------------------------------------------------------------- scenarios


def _round(at: int, **settings: Any) -> dict[str, Any]:
    return {"op": "round", "at": at, "set": settings}


def _rounds(start: int, stop: int, step: int = 60, **settings: Any) -> list[Any]:
    steps = [_round(at) for at in range(start, stop + 1, step)]
    if steps and settings:
        steps[0]["set"] = settings
    return steps


def _respawn(at: int, **settings: Any) -> dict[str, Any]:
    return {"op": "respawn", "at": at, "set": settings}


def _seed(payload: dict[str, Any]) -> dict[str, Any]:
    return {"op": "seed", "payload_json": json.dumps(payload)}


def _scenario(name: str, steps: list[Any], **config: Any) -> dict[str, Any]:
    flat: list[Any] = []
    pending = list(reversed(steps))
    while pending:
        step = pending.pop()
        if isinstance(step, list):
            pending.extend(reversed(step))
        else:
            flat.append(step)
    return {"name": name, "config": _config(**config), "steps": flat}


def _held(check_id: int, data_json: str = INTERNET, **fields: Any) -> dict[str, Any]:
    row = {
        "check_id": check_id,
        "data_json": data_json,
        "status": "idle",
        "disabled": False,
        "next_check_time": T0 + 100_000,
        "held_since": T0 + 200,
    }
    row.update(fields)
    return row


def _outage(path: str, down_at: int, up_at: int, **settings: Any) -> list[Any]:
    """``path`` fails from ``down_at`` and answers again from ``up_at``."""
    return [
        _rounds(down_at, up_at - 60, down_paths=[path], **settings),
        _round(up_at, down_paths=[]),
    ]


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "incident_type": "site_connectivity",
        "version": 1,
        "phase": "idle",
        "incident_id": 0,
        "observed_at": T0,
        "paths": {
            name: {
                "state": "up",
                "down_since": 0,
                "recovered_at": 0,
                "release_at": 0,
                "last_release_at": 0,
            }
            for name in ("dns", "ipv4", "ipv6")
        },
        "ongoing": {},
        "summaries": [],
        "recheck": {},
    }
    payload.update(overrides)
    return payload


def _summary(index: int, **overrides: Any) -> dict[str, Any]:
    start = T0 + 10_000 * index
    record: dict[str, Any] = {
        "incident_id": start,
        "started_at": start,
        "ended_at": start + 1200,
        "paths": {"ipv4": {"down_since": start, "recovered_at": start + 1200}},
        "ongoing_delivered": False,
        "held_at_close": index,
        "attempt_at": 0,
        "attempts": 0,
    }
    record.update(overrides)
    return record


def _state_machine() -> list[dict[str, Any]]:
    return [
        _scenario(
            "single_failing_target_never_takes_a_path_down",
            [_rounds(T0 + 60, T0 + 300, down_targets=["1.1.1.1:443"])],
        ),
        _scenario(
            "failing_needs_two_rounds_then_reconnect_is_silent",
            [
                _round(T0 + 60, down_paths=["ipv4"]),
                _round(T0 + 120),
                _rounds(T0 + 180, T0 + 240),
                _round(T0 + 300, down_paths=[]),
                _rounds(T0 + 360, T0 + 1260),
            ],
        ),
        _scenario(
            "failing_returns_to_up",
            [_round(T0 + 60, down_paths=["dns"]), _round(T0 + 120, down_paths=[])],
        ),
        _scenario(
            "down_after_one_failure",
            [_outage("ipv6", T0 + 60, T0 + 180), _rounds(T0 + 240, T0 + 360)],
            down_after_failures=1,
            recovery_grace_seconds=120,
        ),
        _scenario(
            "failed_round_at_the_release_boundary_keeps_the_incident",
            [
                _outage("ipv4", T0 + 60, T0 + 180),
                _rounds(T0 + 240, T0 + 1020),
                _round(T0 + 1080, down_paths=["ipv4"]),
                _round(T0 + 1140, down_paths=[]),
                _rounds(T0 + 1200, T0 + 2040),
            ],
        ),
        _scenario(
            "flap_during_grace_needs_a_clean_grace",
            [
                _outage("ipv4", T0 + 60, T0 + 180),
                _rounds(T0 + 240, T0 + 600),
                _round(T0 + 660, down_paths=["ipv4"]),
                _round(T0 + 720, down_paths=[]),
                _rounds(T0 + 780, T0 + 1680, 120),
            ],
        ),
        _scenario(
            "staggered_paths_close_once",
            [
                _round(T0 + 60, down_paths=["ipv4", "dns"]),
                _round(T0 + 120),
                _round(T0 + 180, down_paths=["ipv4"]),
                _rounds(T0 + 240, T0 + 600),
                _round(T0 + 660, down_paths=[]),
                _rounds(T0 + 720, T0 + 1800, 120),
            ],
        ),
        _scenario(
            "raising_probe_counts_as_a_failed_target",
            [
                _rounds(
                    T0 + 60,
                    T0 + 240,
                    raise_targets=["cloudflare.com"],
                ),
                _round(T0 + 300, raise_targets=[]),
            ],
        ),
        _scenario(
            "no_targets_at_all",
            [_rounds(T0 + 60, T0 + 180), _respawn(T0 + 200), _round(T0 + 240)],
            ipv4=[],
            ipv6=[],
            dns=[],
        ),
        _scenario(
            "sub_minute_outage",
            [
                _round(T0 + 60, down_paths=["dns"]),
                _rounds(T0 + 90, T0 + 210, 30, down_paths=[]),
            ],
            down_after_failures=1,
            recovery_grace_seconds=30,
            incident_notify_after_seconds=20,
        ),
        _scenario(
            "ipv6_unobserved_internet_still_holds_on_ipv4",
            [
                _outage("ipv4", T0 + 60, T0 + 1140, held=[_held(5)]),
                _rounds(T0 + 1200, T0 + 2100, 300),
            ],
            ipv6=[],
        ),
    ]


def _ongoing() -> list[dict[str, Any]]:
    return [
        _scenario(
            "summary_threshold_below",
            [_outage("ipv4", T0 + 60, T0 + 1019 - 59), _rounds(T0 + 1020, T0 + 2000, 120)],
            incident_notify_after_seconds=900,
        ),
        _scenario(
            "summary_threshold_at",
            [_outage("ipv4", T0 + 60, T0 + 1020), _rounds(T0 + 1080, T0 + 2100, 120)],
        ),
        _scenario(
            "long_outage_alerts_intent_first_and_retires",
            [
                _outage("ipv4", T0 + 60, T0 + 2400, deliver="failed"),
                _rounds(T0 + 2460, T0 + 3420, 60, deliver="delivered"),
            ],
        ),
        _scenario(
            "delivered_alert_reminds_after_the_interval",
            [_outage("dns", T0 + 60, T0 + 5400), _rounds(T0 + 5460, T0 + 6600, 300)],
            incident_reminder_seconds=1800,
        ),
        _scenario(
            "retired_alert_allows_a_new_attempt_after_the_reminder",
            [
                _rounds(T0 + 60, T0 + 1020, down_paths=["ipv4"], deliver="failed"),
                _round(T0 + 1080, down_paths=[]),
                _round(T0 + 1140, down_paths=["ipv4"]),
                _rounds(T0 + 1200, T0 + 1500),
                _rounds(T0 + 2700, T0 + 3000, 60, deliver="delivered"),
                _round(T0 + 3060, down_paths=[]),
                _rounds(T0 + 3120, T0 + 4200, 180),
            ],
            incident_reminder_seconds=1800,
        ),
        _scenario(
            "ipv6_only_outage_and_all_paths_down",
            [
                _outage("ipv6", T0 + 60, T0 + 1080),
                _rounds(T0 + 1140, T0 + 2100, 120),
                _rounds(T0 + 2160, T0 + 3300, 60, down_paths=["dns", "ipv4", "ipv6"]),
                _round(T0 + 3360, down_paths=[]),
                _rounds(T0 + 3420, T0 + 4500, 180),
            ],
        ),
        _scenario(
            "notifier_outcomes",
            [
                _rounds(T0 + 60, T0 + 1020, down_paths=["ipv4"], deliver="raise"),
                _round(T0 + 1080, deliver="unknown"),
                _rounds(T0 + 1140, T0 + 1260),
                _round(T0 + 1320, down_paths=[]),
                _rounds(T0 + 1380, T0 + 2280, 60, deliver="raise"),
                _rounds(T0 + 2340, T0 + 2460, 60, deliver="unknown"),
            ],
        ),
    ]


def _summaries() -> list[dict[str, Any]]:
    oversized = _summary(
        0,
        paths={
            f"path{index:03d}": {"down_since": T0, "recovered_at": T0 + 1200}
            for index in range(200)
        },
        ongoing_delivered=True,
        held_at_close=3,
    )
    giant = _summary(1, paths={"x" * 4000: {"down_since": 5, "recovered_at": "7"}})
    return [
        _scenario(
            "summary_is_retried_and_appended_only_once",
            [
                _outage("ipv4", T0 + 60, T0 + 2400, deliver="failed"),
                _rounds(T0 + 2460, T0 + 3300),
                _respawn(T0 + 3320),
                _round(T0 + 3330),
                _round(T0 + 3420, deliver="delivered"),
                _rounds(T0 + 3600, T0 + 3900, 150),
            ],
        ),
        _scenario(
            "second_outage_while_a_summary_is_pending",
            [
                _outage("ipv4", T0 + 60, T0 + 1140, deliver="failed"),
                _rounds(T0 + 1200, T0 + 2040),
                _outage("dns", T0 + 2100, T0 + 3180),
                _rounds(T0 + 3240, T0 + 4020),
                _round(T0 + 4080, deliver="delivered"),
            ],
        ),
        _scenario(
            "backlog_drains_in_bounded_batches",
            [
                _seed(_payload(summaries=[_summary(i) for i in range(1, 14)])),
                _respawn(T0 + 60),
                _rounds(T0 + 60, T0 + 300),
            ],
        ),
        _scenario(
            "failed_batch_touches_only_its_records",
            [
                _seed(_payload(summaries=[_summary(i) for i in range(1, 9)])),
                _respawn(T0 + 60, deliver="failed"),
                _round(T0 + 60),
                _round(T0 + 119, deliver="delivered"),
                _round(T0 + 120),
                _round(T0 + 180),
            ],
        ),
        _scenario(
            "appended_record_keeps_the_retry_bound",
            [
                _seed(_payload(summaries=[_summary(1)])),
                _respawn(T0 + 60, deliver="failed"),
                _round(T0 + 60),
                _seed(
                    _payload(
                        summaries=[_summary(1, attempt_at=T0 + 60, attempts=1), _summary(2)]
                    )
                ),
                _respawn(T0 + 75, deliver="delivered"),
                _round(T0 + 75),
                _round(T0 + 119),
                _round(T0 + 120),
            ],
        ),
        _scenario(
            "oversized_records_are_cut_down",
            [
                _seed(_payload(summaries=[giant, oversized, _summary(2)])),
                _respawn(T0 + 60),
                _rounds(T0 + 60, T0 + 240),
            ],
        ),
        _scenario(
            "character_budget_splits_batches",
            [
                _seed(
                    _payload(
                        summaries=[
                            _summary(
                                i,
                                paths={
                                    f"p{i}-{n:02d}": {"down_since": 1, "recovered_at": 2}
                                    for n in range(14)
                                },
                            )
                            for i in range(1, 7)
                        ]
                    )
                ),
                _respawn(T0 + 60),
                _rounds(T0 + 60, T0 + 300),
            ],
        ),
        _scenario(
            "summary_counts_held_checks",
            [
                _outage("ipv4", T0 + 60, T0 + 1140, held_at_close=7),
                _rounds(T0 + 1200, T0 + 2040, 60, deliver="failed"),
                _round(T0 + 2100, fail_count_held=True, held_at_close=None),
            ],
        ),
        _scenario(
            "held_count_failure_records_zero",
            [
                _outage("ipv4", T0 + 60, T0 + 1140, fail_count_held=True),
                _rounds(T0 + 1200, T0 + 2100, 60),
            ],
        ),
    ]


def _restarts() -> list[dict[str, Any]]:
    return [
        _scenario(
            "restart_during_an_outage",
            [
                _rounds(T0 + 60, T0 + 1020, down_paths=["ipv4"], deliver="failed"),
                _respawn(T0 + 1050),
                _round(T0 + 1070),
                _round(T0 + 1080),
            ],
        ),
        _scenario(
            "restart_during_grace",
            [
                _outage("ipv4", T0 + 60, T0 + 180),
                _respawn(T0 + 200),
                _round(T0 + 1020),
                _round(T0 + 1080),
            ],
        ),
        _scenario(
            "restart_after_close_continues_the_recheck",
            [
                _outage("ipv4", T0 + 60, T0 + 180),
                _round(T0 + 1080, in_flight=1),
                _respawn(
                    T0 + 1100,
                    in_flight=0,
                    held=[
                        _held(11, next_check_time=T0 + 4680),
                        _held(12, status="processing", next_check_time=T0 + 4680),
                    ],
                ),
                _round(T0 + 1140),
                _round(T0 + 1200, held=[]),
            ],
        ),
        _scenario(
            "restart_after_a_long_disable_drops_the_stale_outage",
            [
                _rounds(T0 + 60, T0 + 1020, down_paths=["ipv4"], deliver="failed"),
                _respawn(T0 + 1020 + 181),
                _round(T0 + 1260, down_paths=[]),
            ],
        ),
        _scenario(
            "stale_downgrade_skips_an_unobserved_path",
            [
                _rounds(T0 + 60, T0 + 1020, down_paths=["dns"], deliver="failed"),
                _respawn(T0 + 1300),
                _round(T0 + 1320, down_paths=[]),
            ],
            ipv6=[],
        ),
        _scenario(
            "restart_at_the_staleness_boundary_keeps_the_outage",
            [
                _rounds(T0 + 60, T0 + 180, down_paths=["ipv4"]),
                _respawn(T0 + 180 + 180),
                _round(T0 + 420),
            ],
        ),
        _scenario(
            "failed_restore_never_writes",
            [
                _outage("ipv4", T0 + 60, T0 + 1320, deliver="failed"),
                _rounds(T0 + 1380, T0 + 2220),
                _respawn(T0 + 2280, fail_reads=True),
                _round(T0 + 2280),
                _round(T0 + 2310),
                _round(T0 + 2340, fail_reads=False, deliver="delivered"),
            ],
        ),
        _scenario(
            "first_round_restores_and_absent_row_counts_as_restored",
            [
                _round(T0 + 60, fail_reads=True),
                _round(T0 + 120, fail_reads=False),
                _respawn(T0 + 130),
                {"op": "drop_row"},
                _round(T0 + 180),
                _round(T0 + 240),
                {"op": "restore", "at": T0 + 250},
            ],
        ),
    ]


def _store_failures() -> list[dict[str, Any]]:
    return [
        _scenario(
            "store_failures_do_not_stop_the_state_machine",
            [
                _round(T0 + 60, fail_writes=True, down_paths=["ipv4"]),
                _rounds(T0 + 120, T0 + 1020),
                _round(T0 + 1080, fail_writes=False),
                _rounds(T0 + 1140, T0 + 1200),
            ],
        ),
        _scenario(
            "failed_intent_write_prevents_the_send",
            [
                _rounds(T0 + 60, T0 + 960, down_paths=["ipv4"]),
                _round(T0 + 1020, fail_write_calls=[1]),
                _round(T0 + 1080, fail_write_calls=[2]),
                _round(T0 + 1140),
                _round(T0 + 1200, down_paths=[]),
                _rounds(T0 + 1260, T0 + 2040),
                _round(T0 + 2100, fail_write_calls=[1]),
                _round(T0 + 2160, fail_write_calls=[2]),
                _round(T0 + 2220),
            ],
        ),
        _scenario(
            "a_vanished_row_is_recreated",
            [
                _round(T0 + 60),
                {"op": "drop_row"},
                _round(T0 + 120),
                _round(T0 + 180),
            ],
        ),
    ]


def _recheck() -> list[dict[str, Any]]:
    blocked = json.dumps({"site_dependency": {"requires": ["dns", "ipv4"]}})
    return [
        _scenario(
            "recheck_reads_in_flight_before_the_held_set",
            [
                _outage("ipv4", T0 + 60, T0 + 180),
                _rounds(
                    T0 + 1080,
                    T0 + 1200,
                    in_flight=2,
                    held=[_held(1), _held(2, IPV6_ONLY), _held(3, '{"x": 1}')],
                ),
                _round(T0 + 1260, in_flight=0),
                _round(T0 + 1320, held=[]),
                _round(T0 + 1380),
            ],
        ),
        _scenario(
            "recheck_skips_checks_blocked_by_another_requirement",
            [
                _round(T0 + 60, down_paths=["ipv4", "dns"]),
                _round(T0 + 120),
                _round(T0 + 180, down_paths=["dns"]),
                _rounds(T0 + 1080, T0 + 1140, held=[_held(7, blocked), _held(8, IPV4_ONLY)]),
                _round(T0 + 1200, down_paths=[]),
                _rounds(T0 + 2100, T0 + 2220),
            ],
        ),
        _scenario(
            "recheck_filters_disabled_and_due_checks_and_bad_dependencies",
            [
                _outage("ipv4", T0 + 60, T0 + 180),
                _round(
                    T0 + 1080,
                    held=[
                        _held(1, disabled=True),
                        _held(2, next_check_time=T0 + 1080),
                        _held(3, next_check_time=T0 + 1081),
                        _held(4, json.dumps({"site_dependency": "nope"})),
                        _held(5, json.dumps({"site_dependency": 7})),
                    ],
                ),
                _round(T0 + 1140),
                _round(T0 + 1200, held=[]),
            ],
        ),
        _scenario(
            "recheck_query_and_reschedule_failures_keep_it_pending",
            [
                _outage("ipv4", T0 + 60, T0 + 180),
                _round(T0 + 1080, fail_in_flight=True, held=[_held(1)]),
                _round(T0 + 1140, fail_in_flight=False, fail_held=True),
                _round(T0 + 1200, fail_held=False, fail_reschedule=True),
                _round(T0 + 1260, fail_reschedule=False),
                _round(T0 + 1320, held=[]),
            ],
        ),
        _scenario(
            "recheck_release_extends_while_another_path_recovers",
            [
                _round(T0 + 60, down_paths=["ipv4", "ipv6"]),
                _round(T0 + 120),
                _round(T0 + 180, down_paths=["ipv6"]),
                _round(T0 + 360, down_paths=[]),
                _rounds(T0 + 1080, T0 + 1320, held=[_held(1)], in_flight=1),
                _round(T0 + 1380, held=[], in_flight=0),
            ],
        ),
        _scenario(
            "observe_mode_never_reschedules",
            [_outage("ipv4", T0 + 60, T0 + 180, held=[_held(1)]), _rounds(T0 + 1080, T0 + 1200)],
            mode="observe",
        ),
        _scenario(
            "off_mode_does_nothing",
            [
                _round(T0 + 60, down_paths=["ipv4"]),
                _round(T0 + 120),
                _respawn(T0 + 180),
            ],
            mode="off",
        ),
    ]


def _restore_payloads() -> list[dict[str, Any]]:
    path = {
        "state": "down",
        "down_since": T0,
        "recovered_at": 0,
        "release_at": 0,
        "last_release_at": 5,
    }
    odd_paths = {
        "dns": {"state": "failing", "down_since": True, "last_release_at": 12.9},
        "ipv4": {**path, "state": "recovering", "recovered_at": T0 + 30, "release_at": T0 + 90},
        "ipv6": "broken",
        "smtp": {"state": "down"},
    }
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("unknown_version", _payload(version=2, phase="active", incident_id=T0), {}),
        ("float_version", _payload(version=1.0, phase="active", incident_id=T0), {}),
        ("true_version", _payload(version=True), {}),
        ("missing_version", {"phase": "active", "incident_id": T0, "observed_at": T0}, {}),
        ("string_version", _payload(version="1"), {}),
        ("odd_paths", _payload(paths=odd_paths, observed_at=T0 + 10.7), {}),
        (
            "invalid_states_and_types",
            _payload(
                paths={
                    "dns": {"state": "unobserved"},
                    "ipv4": {"state": 3},
                    "ipv6": {"state": "DOWN"},
                },
                phase="ACTIVE",
                ongoing=[1],
                summaries={"a": 1},
                recheck="pending",
                observed_at="T0",
                incident_id=None,
            ),
            {},
        ),
        (
            "kept_unknown_keys",
            _payload(
                phase="active",
                incident_id=T0 + 0.0,
                paths={"ipv4": path},
                ongoing={
                    "incident_id": float(T0),
                    "attempt_at": T0 - 100,
                    "attempts": 2,
                    "delivered": 0,
                    "retired": [],
                    "last_alert_at": False,
                    "note": "kept",
                },
                summaries=[_summary(1, extra="kept"), 5, None, _summary(2)],
                recheck={"pending": 1, "release_at": T0 - 50, "extra": "dropped later"},
            ),
            {"down_paths": ["ipv4"], "in_flight": 1},
        ),
        (
            "bool_incident_id_matches_one",
            _payload(
                phase="active",
                incident_id=True,
                paths={"ipv4": {**path, "down_since": 1}},
                ongoing={"incident_id": 1, "delivered": True, "last_alert_at": T0},
            ),
            {"down_paths": ["ipv4"]},
        ),
        (
            "infinite_timestamp_raises",
            _payload(paths={"ipv4": {**path, "recovered_at": float("inf")}}),
            {},
        ),
        (
            "nan_in_summary_raises_during_the_round",
            _payload(summaries=[_summary(1, attempt_at=float("nan"))]),
            {},
        ),
    ]
    scenarios = []
    for name, payload, settings in cases:
        scenarios.append(
            _scenario(
                f"restore_{name}",
                [
                    _seed(payload),
                    _respawn(T0 + 60, **settings),
                    _round(T0 + 70),
                    _round(T0 + 130),
                ],
            )
        )
    return scenarios


def _group(builder: Callable[[], list[dict[str, Any]]]) -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        return {"scenarios": [_replay(scenario) for scenario in builder()]}

    return build


GROUPS: list[Callable[[], list[dict[str, Any]]]] = [
    _state_machine,
    _ongoing,
    _summaries,
    _restarts,
    _store_failures,
    _recheck,
    _restore_payloads,
]

#: Observer code the replay does not reach, with the reason. Everything else
#: of the observer's methods and of the module's rendering helpers must be
#: executed by the corpus.
NOT_REPLAYED = {
    "__init__": "default probe runner (I/O driver, S11)",
    "unobserved_paths": "startup wiring (S11)",
    "warn_unobserved_requirements": "startup wiring (S11)",
    "stop": "loop (S11)",
    "run": "loop (S11)",
    "_wait_for_next_round": "loop (S11)",
    "_send": "notifier missing (I/O driver, S11)",
    "_store_get": "async store variant (I/O driver)",
    "_store_open": "async store variant (I/O driver)",
    "_store_set": "async store variant (I/O driver)",
    "_count_held_checks": "sync scheduler fallback (I/O driver)",
}

#: Single unreachable lines (by source text) of otherwise replayed functions.
NOT_REPLAYED_LINES = {
    # A line without any path detail fits the budget unless an integer has
    # thousands of digits, which the agent never writes.
    "_summary_line": "line = line[:limit]",
}


def build_coverage() -> dict[str, Any]:
    """Replay every scenario under line coverage of the observer code."""
    import sys

    from nyxmon.adapters import site_connectivity as module

    functions: dict[str, Any] = {
        name: getattr(value, "__func__", value)
        for name, value in vars(module.SiteConnectivityObserver).items()
        if hasattr(getattr(value, "__func__", value), "__code__")
    }
    for helper in (
        module._iso,
        module._duration,
        module._as_mapping,
        module._as_int,
        module._render_summary_line,
        module._summary_line,
        module._record_is_due,
    ):
        functions[helper.__name__] = helper

    def code_lines(code: Any) -> set[int]:
        lines = {line for _, _, line in code.co_lines() if line is not None}
        for const in code.co_consts:
            if hasattr(const, "co_lines"):
                lines |= code_lines(const)
        return lines - {code.co_firstlineno}

    monitoring = sys.monitoring
    tool = next(t for t in range(6) if monitoring.get_tool(t) is None)
    monitoring.use_tool_id(tool, "agent-rs site replay coverage")
    executed: set[int] = set()

    def on_line(code: Any, line: int) -> None:
        if code.co_filename == module.__file__:
            executed.add(line)

    try:
        monitoring.register_callback(tool, monitoring.events.LINE, on_line)
        monitoring.set_events(tool, monitoring.events.LINE)
        for group in GROUPS:
            for scenario in group():
                _replay(scenario)
    finally:
        monitoring.set_events(tool, 0)
        monitoring.register_callback(tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(tool)

    import linecache

    def exempt(name: str, line: int) -> bool:
        text = linecache.getline(module.__file__, line).strip()
        return NOT_REPLAYED_LINES.get(name) == text

    missed = {
        name: sorted(
            line
            for line in code_lines(function.__code__) - executed
            if not exempt(name, line)
        )
        for name, function in functions.items()
    }
    unexpected = {
        name: lines
        for name, lines in missed.items()
        if lines and name not in NOT_REPLAYED
    }
    if unexpected:
        raise AssertionError(f"site replay corpus misses observer lines {unexpected}")
    return {
        "replayed": sorted(name for name, lines in missed.items() if not lines),
        "not_replayed": NOT_REPLAYED,
        "not_replayed_lines": NOT_REPLAYED_LINES,
    }


GENERATORS: dict[str, Callable[[], dict[str, Any]]] = {
    "site_replay/coverage.json": build_coverage,
    "site_replay/state_machine.json": _group(_state_machine),
    "site_replay/ongoing.json": _group(_ongoing),
    "site_replay/summaries.json": _group(_summaries),
    "site_replay/restarts.json": _group(_restarts),
    "site_replay/store_failures.json": _group(_store_failures),
    "site_replay/recheck.json": _group(_recheck),
    "site_replay/restore_payloads.json": _group(_restore_payloads),
}
