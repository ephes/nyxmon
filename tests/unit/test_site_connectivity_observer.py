"""Site connectivity observer, incident coordinator and recovery recheck.

Every worked example of ``docs/site-connectivity-plan.md`` section 12 that
concerns the observer is pinned here, driven by an injected clock and a
scripted probe runner so nothing sleeps and nothing depends on wall time:

* a three-minute reconnect produces zero notifications and still closes;
* a 40-minute outage produces one ongoing alert intent before every send, is
  retired on recovery, and yields exactly one summary at close;
* a flap during the grace re-opens ``down`` and needs one clean grace;
* restarts in the middle of an outage, of a grace and of an unfinished recheck
  continue from the persisted row alone;
* the recovery recheck reads the in-flight claim count *before* the held set.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import pytest

from nyxmon.adapters.repositories.interface import CollectorIncident
from nyxmon.adapters.site_connectivity import (
    PHASE_ACTIVE,
    PHASE_IDLE,
    REASON_DEPENDENCY_DOWN,
    REASON_DEPENDENCY_RECOVERING,
    REASON_MEASURED_BEFORE_RELEASE,
    SITE_DELIVERY_RETRY_SECONDS,
    SITE_INCIDENT_KEY,
    SITE_ONGOING_ERROR_TYPE,
    SITE_SUMMARY_ERROR_TYPE,
    SITE_SUMMARY_MAX_CHARS,
    SITE_SUMMARY_MAX_RECORDS,
    PathSnapshot,
    PathState,
    ProbeTarget,
    SiteConnectivityConfig,
    SiteConnectivityObserver,
    SiteConnectivitySnapshot,
    SiteMode,
    reset_site_config_warning_state,
    warn_unobserved_requirements,
)
from nyxmon.service_layer.site_dependency import (
    SiteDependency,
    reset_dependency_warning_state,
    resolve_site_dependency,
)

T0 = 1_700_000_000
GRACE = 900
NOTIFY_AFTER = 900

IPV4_TARGETS = (
    ProbeTarget(path="ipv4", kind="tcp", host="1.1.1.1", port=443),
    ProbeTarget(path="ipv4", kind="tcp", host="8.8.8.8", port=443),
)
IPV6_TARGETS = (
    ProbeTarget(path="ipv6", kind="tcp", host="2606:4700:4700::1111", port=443),
)
DNS_TARGETS = (ProbeTarget(path="dns", kind="dns", host="cloudflare.com"),)


@pytest.fixture(autouse=True)
def _reset_warning_state():
    reset_site_config_warning_state()
    reset_dependency_warning_state()
    yield
    reset_site_config_warning_state()
    reset_dependency_warning_state()


def make_config(**overrides: Any) -> SiteConnectivityConfig:
    values: dict[str, Any] = {
        "mode": SiteMode.ENFORCE,
        "probe_interval": 60,
        "probe_timeout": 3,
        "ipv4_targets": IPV4_TARGETS,
        "ipv6_targets": IPV6_TARGETS,
        "dns_names": DNS_TARGETS,
        "down_after_failures": 2,
        "recovery_grace_seconds": GRACE,
        "max_hold_seconds": 10800,
        "incident_notify_after_seconds": NOTIFY_AFTER,
        "incident_reminder_seconds": 21600,
    }
    values.update(overrides)
    return SiteConnectivityConfig(**values)


# ------------------------------------------------------------------- fakes


class FakeIncidentStore:
    """In-memory stand-in for the collector incident row."""

    def __init__(self) -> None:
        self.rows: dict[str, CollectorIncident] = {}
        self.writes: list[dict[str, Any]] = []
        self.fail_writes = False
        self.fail_reads = False
        self.claims = 0

    def get_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        if self.fail_reads:
            raise RuntimeError("store is unavailable")
        return self.rows.get(incident_key)

    def open_collector_incident(
        self, incident_key: str, *, now: int, payload: dict[str, Any]
    ) -> CollectorIncident:
        if self.fail_writes:
            raise RuntimeError("store is unavailable")
        existing = self.rows.get(incident_key)
        incident = CollectorIncident(
            incident_key=incident_key,
            opened_at=existing.opened_at if existing else now,
            last_alert_at=existing.last_alert_at if existing else 0,
            alert_count=existing.alert_count if existing else 0,
            payload=copy.deepcopy(payload),
        )
        self.rows[incident_key] = incident
        self.writes.append(copy.deepcopy(payload))
        return incident

    def set_collector_incident_payload(
        self, incident_key: str, payload: dict[str, Any]
    ) -> CollectorIncident | None:
        if self.fail_writes:
            raise RuntimeError("store is unavailable")
        existing = self.rows.get(incident_key)
        if existing is None:
            return None
        incident = CollectorIncident(
            incident_key=existing.incident_key,
            opened_at=existing.opened_at,
            last_alert_at=existing.last_alert_at,
            alert_count=existing.alert_count,
            payload=copy.deepcopy(payload),
        )
        self.rows[incident_key] = incident
        self.writes.append(copy.deepcopy(payload))
        return incident

    def close_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        return self.rows.pop(incident_key, None)

    def claim_collector_incident_alert(self, incident_key: str, **kwargs: Any) -> Any:
        self.claims += 1
        raise AssertionError("the site incident must never claim an alert")

    # helpers -------------------------------------------------------------
    @property
    def payload(self) -> dict[str, Any]:
        row = self.rows.get(SITE_INCIDENT_KEY)
        return dict(row.payload) if row is not None else {}


@dataclass
class FakeHeldCheck:
    check_id: int
    data: dict[str, Any]
    status: str = "idle"
    disabled: bool = False
    next_check_time: int = 0
    held_since: int = 0


class FakeScheduler:
    """Records the recheck queries and their order."""

    def __init__(self) -> None:
        self.held: list[FakeHeldCheck] = []
        self.in_flight = 0
        self.rescheduled: list[tuple[tuple[int, ...], int]] = []
        self.calls: list[str] = []
        self.held_at_close = 0
        self.fail_reschedule = False
        self.on_count: Callable[[], None] | None = None
        self.count_held_calls: list[str] = []

    async def list_held_checks_async(self) -> Sequence[FakeHeldCheck]:
        self.calls.append("held")
        return list(self.held)

    async def reschedule_checks_async(
        self, check_ids: list[int], *, run_at: int
    ) -> int:
        if self.fail_reschedule:
            raise RuntimeError("database is busy")
        self.rescheduled.append((tuple(check_ids), run_at))
        return len(check_ids)

    async def count_held_checks_async(self) -> int:
        self.count_held_calls.append("async")
        return self.held_at_close or len(self.held)

    def count_held_checks(self) -> int:
        # The real repositories enter the blocking portal here, which the
        # observer's event loop must never do.
        self.count_held_calls.append("sync")
        return self.held_at_close or len(self.held)

    async def count_processing_claims_before_async(self, epoch: int) -> int:
        self.calls.append("in_flight")
        if self.on_count is not None:
            self.on_count()
        return self.in_flight


@dataclass
class SentMessage:
    incident_key: str
    name: str
    error_type: str
    error_msg: str
    status: str
    opsgate_ticket: bool
    at: int
    payload_at_send: dict[str, Any] = field(default_factory=dict)


class FakeNotifier:
    """Scripted notifier that records what the store looked like at send time."""

    def __init__(
        self,
        *,
        deliver: bool | Callable[[], bool] = True,
        clock: dict[str, int] | None = None,
        store: FakeIncidentStore | None = None,
    ) -> None:
        self._deliver = deliver
        self._clock = clock if clock is not None else {"now": 0}
        self._store = store
        self.sent: list[SentMessage] = []
        self.raises = False

    def __call__(
        self,
        incident_key: str,
        name: str,
        error_type: str,
        error_msg: str,
        status: str,
        opsgate_ticket: bool,
    ) -> bool:
        self.sent.append(
            SentMessage(
                incident_key=incident_key,
                name=name,
                error_type=error_type,
                error_msg=error_msg,
                status=status,
                opsgate_ticket=opsgate_ticket,
                at=self._clock["now"],
                payload_at_send=(
                    dict(self._store.payload) if self._store is not None else {}
                ),
            )
        )
        if self.raises:
            raise RuntimeError("telegram exploded")
        deliver = self._deliver
        return deliver() if callable(deliver) else deliver

    def of_type(self, error_type: str) -> list[SentMessage]:
        return [message for message in self.sent if message.error_type == error_type]


class FakeProbeRunner:
    """Fails whole paths, or single targets, on demand."""

    def __init__(self) -> None:
        self.down_paths: set[str] = set()
        self.down_targets: set[str] = set()
        self.probed: list[str] = []

    async def probe(self, target: ProbeTarget) -> bool:
        self.probed.append(str(target))
        if str(target) in self.down_targets:
            return False
        return target.path not in self.down_paths


class Harness:
    """One observer with its fakes; ``respawn`` models a process restart."""

    def __init__(self, config: SiteConnectivityConfig | None = None) -> None:
        self.clock: dict[str, int] = {"now": T0}
        self.store = FakeIncidentStore()
        self.scheduler = FakeScheduler()
        self.probes = FakeProbeRunner()
        self.notifier = FakeNotifier(clock=self.clock, store=self.store)
        self.config = config if config is not None else make_config()
        self.observer = self._build()

    def _build(self) -> SiteConnectivityObserver:
        return SiteConnectivityObserver(
            self.config,
            incident_store=self.store,
            scheduler=self.scheduler,
            probe_runner=self.probes,
            clock=lambda: self.clock["now"],
            notifier=self.notifier,
        )

    async def round(self, at: int) -> None:
        self.clock["now"] = at
        await self.observer.run_once()

    async def rounds(self, start: int, stop: int, step: int = 60) -> None:
        for at in range(start, stop + 1, step):
            await self.round(at)

    async def respawn(self, at: int) -> SiteConnectivityObserver:
        """A fresh process over the same persisted row."""
        self.clock["now"] = at
        self.observer = self._build()
        await self.observer.restore()
        return self.observer

    @property
    def payload(self) -> dict[str, Any]:
        return self.store.payload


def snapshot_of(**states: Any) -> SiteConnectivitySnapshot:
    """Build a snapshot directly, for the hold-reason matrix."""
    paths = {}
    for name in ("dns", "ipv4", "ipv6"):
        value = states.get(name, PathState.UP)
        if isinstance(value, PathSnapshot):
            paths[name] = value
        else:
            paths[name] = PathSnapshot(state=str(value))
    return SiteConnectivitySnapshot(
        paths=paths,
        observed_at=states.get("observed_at", T0),
        mode=states.get("mode", SiteMode.ENFORCE),
        incident_id=states.get("incident_id", 0),
    )


INTERNET = SiteDependency((("dns",), ("ipv4", "ipv6")))
IPV6_ONLY = SiteDependency((("ipv6",),))
IPV4_ONLY = SiteDependency((("ipv4",),))


# ----------------------------------------------------------- state machine


@pytest.mark.anyio
async def test_single_failing_target_never_takes_a_path_down() -> None:
    """One dead server is not evidence of an outage (plan 5.1)."""
    harness = Harness()
    harness.probes.down_targets = {"1.1.1.1:443"}

    await harness.rounds(T0 + 60, T0 + 600)

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.UP
    assert snapshot.incident_id == 0
    assert harness.notifier.sent == []
    assert harness.payload["phase"] == PHASE_IDLE


@pytest.mark.anyio
async def test_failing_needs_two_rounds_before_down() -> None:
    """Holding starts at ``down``, never at the first bad round."""
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}

    await harness.round(T0 + 60)
    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.FAILING
    assert snapshot.incident_id == 0

    await harness.round(T0 + 120)
    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.DOWN
    assert snapshot.paths["ipv4"].down_since == T0 + 120
    assert snapshot.incident_id == T0 + 120
    assert harness.payload["phase"] == PHASE_ACTIVE


@pytest.mark.anyio
async def test_failing_returns_to_up_without_an_incident() -> None:
    harness = Harness()
    harness.probes.down_paths = {"dns"}
    await harness.round(T0 + 60)
    harness.probes.down_paths = set()
    await harness.round(T0 + 120)

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["dns"].state == PathState.UP
    assert harness.payload["phase"] == PHASE_IDLE
    assert harness.payload["summaries"] == []


@pytest.mark.anyio
async def test_three_minute_reconnect_is_silent(caplog) -> None:
    """Plan section 12: a short reconnect produces zero notifications."""
    harness = Harness()
    harness.probes.down_paths = {"dns", "ipv4", "ipv6"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)  # confirmed down
    harness.probes.down_paths = set()
    await harness.round(T0 + 180)  # recovering, release at T0 + 1080

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.RECOVERING
    assert snapshot.paths["ipv4"].release_at == T0 + 180 + GRACE

    await harness.rounds(T0 + 240, T0 + 1080)

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert {path.state for path in snapshot.paths.values()} == {str(PathState.UP)}
    assert snapshot.incident_id == 0
    payload = harness.payload
    assert payload["phase"] == PHASE_IDLE
    # Outage duration excludes the grace: 180 - 120 = 60 s, below notify_after.
    assert payload["summaries"] == []
    assert harness.notifier.sent == []
    # The release watermark is kept permanently. The recheck was opened at
    # close and completed in the same round: nothing was held, no claim was in
    # flight (plan section 12, "recheck at t=18: OK -> cleared").
    assert payload["paths"]["ipv4"]["last_release_at"] == T0 + 180 + GRACE
    assert payload["recheck"] == {"pending": False, "release_at": T0 + 180 + GRACE}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outage_seconds", "expect_summary"),
    [(NOTIFY_AFTER - 1, False), (NOTIFY_AFTER, True)],
)
async def test_summary_threshold_boundary(
    outage_seconds: int, expect_summary: bool
) -> None:
    """A summary needs an outage of at least ``incident_notify_after``."""
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 100)
    await harness.round(T0 + 160)  # down_since = T0 + 160
    recovered_at = T0 + 160 + outage_seconds
    harness.probes.down_paths = set()
    await harness.round(recovered_at)
    await harness.round(recovered_at + GRACE)

    payload = harness.payload
    assert payload["phase"] == PHASE_IDLE
    if expect_summary:
        assert len(harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)) == 1
        assert payload["summaries"] == []
    else:
        assert harness.notifier.sent == []
        assert payload["summaries"] == []


# ------------------------------------------------------ 40 minute outage


async def _forty_minute_outage(harness: Harness) -> None:
    """Drive the plan's 40-minute example up to the reconnect."""
    harness.probes.down_paths = {"dns", "ipv4", "ipv6"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)  # down_since = incident_id = T0 + 120
    await harness.rounds(T0 + 180, T0 + 2340)


@pytest.mark.anyio
async def test_long_outage_alerts_once_intent_first_and_retires_on_recovery() -> None:
    """Plan section 12: intent before I/O, retry while down, retire on recovery."""
    harness = Harness()
    harness.notifier._deliver = lambda: harness.clock["now"] >= T0 + 2400

    await _forty_minute_outage(harness)

    outage_alerts = harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)
    # Eligible from incident_id + 900 = T0 + 1020, then every 60 s while down.
    assert [message.at for message in outage_alerts] == list(
        range(T0 + 1020, T0 + 2341, 60)
    )
    for index, message in enumerate(outage_alerts, start=1):
        # The intent was durable before the send.
        ongoing = message.payload_at_send["ongoing"]
        assert ongoing == {
            "incident_id": T0 + 120,
            "attempt_at": message.at,
            "attempts": index,
            "delivered": False,
            "retired": False,
            "last_alert_at": 0,
        }
        assert message.status == "error"
        assert message.opsgate_ticket is True
        assert "down since" in message.error_msg

    # Reconnect: the last down path becomes recovering and the undelivered
    # alert is retired in the same write, although the notifier now succeeds.
    harness.probes.down_paths = set()
    await harness.round(T0 + 2400)
    payload = harness.payload
    assert payload["ongoing"]["retired"] is True
    assert payload["ongoing"]["delivered"] is False

    await harness.rounds(T0 + 2460, T0 + 3300)

    assert harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE) == outage_alerts
    summaries = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    assert len(summaries) == 1
    assert summaries[0].status == "warning"
    assert summaries[0].opsgate_ticket is False
    assert summaries[0].at == T0 + 3300
    assert harness.payload["phase"] == PHASE_IDLE
    assert harness.payload["summaries"] == []


@pytest.mark.anyio
async def test_delivered_ongoing_alert_is_not_repeated_before_the_reminder() -> None:
    harness = Harness()
    harness.probes.down_paths = {"ipv6"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 3000)

    outage_alerts = harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)
    assert [message.at for message in outage_alerts] == [T0 + 1020]
    ongoing = harness.payload["ongoing"]
    assert ongoing["delivered"] is True
    assert ongoing["last_alert_at"] == T0 + 1020
    assert ongoing["attempts"] == 1


@pytest.mark.anyio
async def test_reminder_follows_the_reminder_interval() -> None:
    harness = Harness(make_config(incident_reminder_seconds=1800))
    harness.probes.down_paths = {"ipv6"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 2880)

    assert [
        message.at for message in harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)
    ] == [T0 + 1020, T0 + 2820]


@pytest.mark.anyio
async def test_retired_alert_is_not_retried_when_a_path_flaps_back() -> None:
    """A retired, never delivered alert stays retired (plan section 6)."""
    harness = Harness()
    harness.notifier._deliver = False
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 1020)
    assert len(harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)) == 1

    harness.probes.down_paths = set()
    await harness.round(T0 + 1080)  # recovering, alert retired
    assert harness.payload["ongoing"]["retired"] is True

    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 1140)  # recovering -> down again
    await harness.rounds(T0 + 1200, T0 + 1800)

    assert len(harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)) == 1
    assert harness.payload["paths"]["ipv4"]["down_since"] == T0 + 120


@pytest.mark.anyio
async def test_a_retired_alert_still_allows_a_new_attempt_after_the_reminder() -> None:
    """A retired intent must not silence the incident for good.

    Reproduction of the review finding: the retired record has
    ``last_alert_at == 0``, so keying the next attempt on that value alone
    made a flap back to ``down`` unalertable forever.
    """
    harness = Harness(make_config(incident_reminder_seconds=1800))
    harness.notifier._deliver = False
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)  # ipv4 down, incident T0 + 120
    await harness.rounds(T0 + 180, T0 + 1020)  # one failed attempt at T0 + 1020
    assert [
        message.at for message in harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)
    ] == [T0 + 1020]

    harness.probes.down_paths = set()
    await harness.round(T0 + 1080)  # recovering: the failed attempt is retired
    assert harness.payload["ongoing"]["retired"] is True
    assert harness.payload["ongoing"]["last_alert_at"] == 0

    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 1140)  # recovering -> down again

    # Before the reminder window has elapsed since the attempt, nothing new.
    await harness.rounds(T0 + 1200, T0 + 2760)
    assert len(harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)) == 1

    # T0 + 1020 + 1800: a new attempt, not a retry of the retired one.
    harness.notifier._deliver = True
    await harness.round(T0 + 2820)

    assert [
        message.at for message in harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)
    ] == [T0 + 1020, T0 + 2820]
    ongoing = harness.payload["ongoing"]
    assert ongoing["retired"] is False
    assert ongoing["delivered"] is True
    assert ongoing["last_alert_at"] == T0 + 2820
    assert ongoing["attempts"] == 2


@pytest.mark.anyio
async def test_a_failed_round_at_the_release_boundary_keeps_the_incident() -> None:
    """The round outcome is applied before the release is considered.

    Reproduction of the review finding: releasing first turned a failed round
    exactly at ``release_at`` into ``failing``, which closed the incident and
    dropped every hold although the path was still broken.
    """
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)  # ipv4 down, incident T0 + 120
    await harness.rounds(T0 + 180, T0 + 1200)
    harness.probes.down_paths = set()
    await harness.round(T0 + 1260)  # recovering, release at T0 + 2160
    assert harness.observer.snapshot().paths["ipv4"].release_at == T0 + 2160

    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 2160)  # a failed round exactly at the boundary

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.DOWN
    assert snapshot.paths["ipv4"].down_since == T0 + 120
    assert snapshot.paths["ipv4"].release_at == 0
    assert snapshot.paths["ipv4"].last_release_at == 0
    assert snapshot.incident_id == T0 + 120
    assert harness.payload["phase"] == PHASE_ACTIVE

    # A successful round at the boundary does release and close.
    harness.probes.down_paths = set()
    await harness.round(T0 + 2220)  # recovering again, release at T0 + 3120
    await harness.round(T0 + 3120)

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.UP
    assert snapshot.paths["ipv4"].last_release_at == T0 + 3120
    assert harness.payload["phase"] == PHASE_IDLE


@pytest.mark.anyio
async def test_flap_during_grace_keeps_the_incident_and_needs_a_clean_grace() -> None:
    """Plan section 12: recovering -> down keeps down_since; duration to t=49."""
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 2340)

    harness.probes.down_paths = set()
    await harness.round(T0 + 2400)  # recovering, release at T0 + 3300
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 2820)  # single failed round -> down again
    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.DOWN
    assert snapshot.paths["ipv4"].down_since == T0 + 120
    assert snapshot.paths["ipv4"].release_at == 0
    assert snapshot.incident_id == T0 + 120

    harness.probes.down_paths = set()
    await harness.round(T0 + 2940)  # fresh grace, release at T0 + 3840
    assert harness.observer.snapshot().paths["ipv4"].release_at == T0 + 3840
    await harness.rounds(T0 + 3000, T0 + 3780)
    assert harness.payload["phase"] == PHASE_ACTIVE  # the old release is not enough

    await harness.round(T0 + 3840)
    payload = harness.payload
    assert payload["phase"] == PHASE_IDLE
    summaries = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    assert len(summaries) == 1
    assert "2820" not in summaries[0].error_msg  # rendered as a duration, not seconds
    assert payload["paths"]["ipv4"]["last_release_at"] == T0 + 3840


@pytest.mark.anyio
async def test_ipv6_only_outage_alerts_over_ipv4_and_summarises() -> None:
    """Plan section 12: a partial outage delivers and therefore summarises."""
    harness = Harness()
    harness.probes.down_paths = {"ipv6"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 1020)

    outage_alerts = harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)
    assert len(outage_alerts) == 1
    assert "ipv6" in outage_alerts[0].error_msg
    assert "ipv4" not in outage_alerts[0].error_msg

    harness.probes.down_paths = set()
    await harness.round(T0 + 1080)
    await harness.rounds(T0 + 1140, T0 + 1980)

    payload = harness.payload
    assert payload["phase"] == PHASE_IDLE
    summaries = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    # The outage lasted 960 s (> 900) and the ongoing alert was delivered.
    assert len(summaries) == 1
    assert "Ongoing alert delivered: yes" in summaries[0].error_msg


@pytest.mark.anyio
async def test_defective_observer_still_reports_itself() -> None:
    """Firewalled probe targets: the 15-minute alert reveals the misconfiguration."""
    harness = Harness()
    harness.probes.down_paths = {"dns", "ipv4", "ipv6"}
    await harness.rounds(T0 + 60, T0 + 1020)

    alerts = harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)
    assert len(alerts) == 1
    for name in ("dns", "ipv4", "ipv6"):
        assert name in alerts[0].error_msg


# ------------------------------------------------------------- summaries


@pytest.mark.anyio
async def test_summary_is_retried_and_appended_only_once() -> None:
    """Crash between the close write and the send: retried, never duplicated."""
    harness = Harness()
    harness.notifier._deliver = False
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 2340)
    harness.probes.down_paths = set()
    await harness.round(T0 + 2400)
    await harness.rounds(T0 + 2460, T0 + 3300)  # closes, summary send fails

    payload = harness.payload
    assert len(payload["summaries"]) == 1
    assert payload["summaries"][0]["attempt_at"] == T0 + 3300
    assert payload["summaries"][0]["incident_id"] == T0 + 120

    # A fresh process finds the pending record and retries it once it is due.
    await harness.respawn(T0 + 3320)
    assert harness.observer.snapshot().incident_id == 0
    await harness.round(T0 + 3330)  # only 30 s later: not due
    assert len(harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)) == 1

    harness.notifier._deliver = True
    await harness.round(T0 + 3360 + SITE_DELIVERY_RETRY_SECONDS)
    assert len(harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)) == 2
    assert harness.payload["summaries"] == []

    # And it is not appended a second time by a later round.
    await harness.rounds(T0 + 3600, T0 + 3900)
    assert len(harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)) == 2


@pytest.mark.anyio
async def test_second_outage_while_a_summary_is_pending_reports_both() -> None:
    """Plan section 12: outage B keeps A's record; one send reports both."""
    harness = Harness()
    harness.notifier._deliver = False
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 1080)
    harness.probes.down_paths = set()
    await harness.round(T0 + 1140)
    await harness.rounds(T0 + 1200, T0 + 2040)  # A closes, summary send fails
    first_incident = T0 + 120
    assert [record["incident_id"] for record in harness.payload["summaries"]] == [
        first_incident
    ]

    # Outage B opens while A's record is still pending.
    harness.probes.down_paths = {"dns"}
    await harness.round(T0 + 2100)
    await harness.round(T0 + 2160)
    second_incident = T0 + 2160
    assert harness.payload["phase"] == PHASE_ACTIVE
    assert [record["incident_id"] for record in harness.payload["summaries"]] == [
        first_incident
    ]

    await harness.rounds(T0 + 2220, T0 + 3120)
    harness.probes.down_paths = set()
    await harness.round(T0 + 3180)
    # Delivery only starts working once B has closed, so one send has to cover
    # both records.
    harness.notifier._deliver = lambda: harness.clock["now"] >= T0 + 4080
    await harness.rounds(T0 + 3240, T0 + 4080)

    assert harness.payload["summaries"] == []
    delivered = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)[-1]
    assert str(first_incident) in delivered.error_msg
    assert str(second_incident) in delivered.error_msg


def _summary_backlog(count: int) -> list[dict[str, Any]]:
    """``count`` pending summary records, oldest first."""
    return [
        {
            "incident_id": T0 + 10_000 * index,
            "started_at": T0 + 10_000 * index,
            "ended_at": T0 + 10_000 * index + 1200,
            "paths": {
                "ipv4": {
                    "down_since": T0 + 10_000 * index,
                    "recovered_at": T0 + 10_000 * index + 1200,
                }
            },
            "ongoing_delivered": False,
            "held_at_close": index,
            "attempt_at": 0,
            "attempts": 0,
        }
        for index in range(1, count + 1)
    ]


def _outage_ids(message: str) -> list[int]:
    return [int(value) for value in re.findall(r"outage (\d+) lasted", message)]


def _seed_summaries(harness: Harness, records: list[dict[str, Any]]) -> None:
    payload = harness.observer.build_payload()
    payload["observed_at"] = T0
    payload["summaries"] = records
    harness.store.open_collector_incident(SITE_INCIDENT_KEY, now=T0, payload=payload)


@pytest.mark.anyio
async def test_a_summary_backlog_drains_in_size_bounded_batches() -> None:
    """A backlog must never become one message Telegram rejects.

    Reproduction of the review finding: 13 records concatenated into a single
    body exceed the 4096 character limit, so the send fails forever and the
    backlog can never drain.
    """
    harness = Harness()
    records = _summary_backlog(13)
    _seed_summaries(harness, records)
    await harness.respawn(T0 + 60)

    await harness.rounds(T0 + 60, T0 + 300)

    messages = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    batches = [_outage_ids(message.error_msg) for message in messages]
    assert [len(batch) for batch in batches] == [5, 5, 3]
    for message in messages:
        assert len(message.error_msg) <= SITE_SUMMARY_MAX_CHARS
        assert len(_outage_ids(message.error_msg)) <= SITE_SUMMARY_MAX_RECORDS
    # No record is lost and none is sent twice; the oldest go first.
    sent = [incident_id for batch in batches for incident_id in batch]
    assert sent == [record["incident_id"] for record in records]
    assert harness.payload["summaries"] == []


@pytest.mark.anyio
async def test_a_failed_batch_does_not_touch_the_records_it_did_not_cover() -> None:
    """Only the batch's own records get an ``attempt_at``."""
    harness = Harness()
    _seed_summaries(harness, _summary_backlog(8))
    await harness.respawn(T0 + 60)
    harness.notifier._deliver = False

    await harness.round(T0 + 60)

    summaries = harness.payload["summaries"]
    assert [record["attempt_at"] for record in summaries] == [T0 + 60] * 5 + [0] * 3
    assert [record["attempts"] for record in summaries] == [1] * 5 + [0] * 3

    # The batch is retried after the delivery retry window, not before.
    await harness.round(T0 + 60 + SITE_DELIVERY_RETRY_SECONDS - 1)
    assert len(harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)) == 1

    harness.notifier._deliver = True
    await harness.round(T0 + 60 + SITE_DELIVERY_RETRY_SECONDS)
    messages = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    assert len(messages) == 2
    assert _outage_ids(messages[1].error_msg) == _outage_ids(messages[0].error_msg)
    assert len(harness.payload["summaries"]) == 3


@pytest.mark.anyio
async def test_an_appended_record_does_not_shorten_the_retry_bound() -> None:
    """Reproduction of the review finding: eligibility is per record.

    A failed batch used to become due again as soon as *any* record of the
    next batch was due, so a summary appended one probe tick later resent the
    failed message far inside the 60 s retry bound.
    """
    harness = Harness()
    records = _summary_backlog(2)
    first, appended = records[0], records[1]
    _seed_summaries(harness, [first])
    await harness.respawn(T0 + 60)
    harness.notifier._deliver = False

    await harness.round(T0 + 60)
    assert len(harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)) == 1
    pending = harness.payload["summaries"]
    assert [record["attempt_at"] for record in pending] == [T0 + 60]

    # A second outage appends its record 15 s after that failed send.
    payload = copy.deepcopy(harness.payload)
    payload["summaries"] = [*pending, appended]
    harness.store.open_collector_incident(
        SITE_INCIDENT_KEY, now=T0 + 75, payload=payload
    )
    await harness.respawn(T0 + 75)
    harness.notifier._deliver = True

    # Sending would now succeed, but the retry bound of the older record holds.
    await harness.round(T0 + 75)
    await harness.round(T0 + 60 + SITE_DELIVERY_RETRY_SECONDS - 1)
    assert len(harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)) == 1
    assert [record["attempt_at"] for record in harness.payload["summaries"]] == [
        T0 + 60,
        0,
    ]

    # Once the bound has elapsed one message covers both records.
    await harness.round(T0 + 60 + SITE_DELIVERY_RETRY_SECONDS)
    messages = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    assert len(messages) == 2
    assert _outage_ids(messages[1].error_msg) == [
        first["incident_id"],
        appended["incident_id"],
    ]
    assert harness.payload["summaries"] == []


@pytest.mark.anyio
async def test_a_single_oversized_summary_record_is_truncated_not_dropped() -> None:
    """A record that alone busts the budget still gets delivered."""
    harness = Harness()
    _seed_summaries(
        harness,
        [
            {
                "incident_id": T0,
                "started_at": T0,
                "ended_at": T0 + 1200,
                "paths": {
                    f"path{index:03d}": {
                        "down_since": T0,
                        "recovered_at": T0 + 1200,
                    }
                    for index in range(200)
                },
                "ongoing_delivered": True,
                "held_at_close": 3,
                "attempt_at": 0,
                "attempts": 0,
            }
        ],
    )
    await harness.respawn(T0 + 60)

    await harness.round(T0 + 60)

    messages = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    assert len(messages) == 1
    body = messages[0].error_msg
    assert len(body) <= SITE_SUMMARY_MAX_CHARS
    assert f"outage {T0} lasted" in body
    assert "more path(s)" in body
    assert harness.payload["summaries"] == []


@pytest.mark.anyio
async def test_summary_records_the_number_of_held_checks() -> None:
    harness = Harness()
    harness.scheduler.held_at_close = 7
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 1080)
    harness.probes.down_paths = set()
    await harness.round(T0 + 1140)
    harness.notifier._deliver = False
    await harness.rounds(T0 + 1200, T0 + 2040)

    record = harness.payload["summaries"][0]
    assert record["held_at_close"] == 7
    assert record["ongoing_delivered"] is True
    assert record["paths"]["ipv4"] == {
        "down_since": T0 + 120,
        "recovered_at": T0 + 1140,
    }
    assert "7 dependent check(s) were held" in harness.notifier.sent[-1].error_msg


# ---------------------------------------------------------------- restarts


@pytest.mark.anyio
async def test_restart_during_an_outage_restores_state_and_the_attempt() -> None:
    """Plan section 12: restart at t=20 of a 40-minute outage."""
    harness = Harness()
    harness.notifier._deliver = False
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 1020)
    assert len(harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)) == 1

    await harness.respawn(T0 + 1050)
    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.observed_at == T0 + 1020
    assert snapshot.incident_id == T0 + 120
    assert snapshot.paths["ipv4"].state == PathState.DOWN
    assert snapshot.paths["ipv4"].down_since == T0 + 120

    # The restored attempt is not repeated before its retry is due.
    await harness.round(T0 + 1070)
    assert len(harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)) == 1
    await harness.round(T0 + 1080)
    assert len(harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)) == 2


@pytest.mark.anyio
async def test_restart_during_grace_honours_the_persisted_release() -> None:
    """Plan section 12: a restart neither releases early nor restarts the grace."""
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    harness.probes.down_paths = set()
    await harness.round(T0 + 180)  # release at T0 + 1080

    await harness.respawn(T0 + 200)
    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.RECOVERING
    assert snapshot.paths["ipv4"].release_at == T0 + 1080

    await harness.round(T0 + 1020)
    assert harness.observer.snapshot().paths["ipv4"].state == PathState.RECOVERING
    await harness.round(T0 + 1080)
    assert harness.observer.snapshot().paths["ipv4"].state == PathState.UP
    assert harness.payload["paths"]["ipv4"]["last_release_at"] == T0 + 1080


@pytest.mark.anyio
async def test_restart_after_close_continues_the_recheck() -> None:
    """Plan section 12: an idle held check is rescheduled after the restart."""
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    harness.probes.down_paths = set()
    await harness.round(T0 + 180)
    harness.scheduler.in_flight = 1  # a pre-release claim is still processing
    await harness.round(T0 + 1080)  # closes, recheck pending
    harness.scheduler.in_flight = 0
    assert harness.payload["recheck"] == {"pending": True, "release_at": T0 + 1080}

    harness.scheduler.held = [
        FakeHeldCheck(
            check_id=11,
            data={"site_dependency": "internet"},
            next_check_time=T0 + 4680,
            held_since=T0 + 200,
        ),
        FakeHeldCheck(
            check_id=12,
            data={"site_dependency": "internet"},
            status="processing",
            next_check_time=T0 + 4680,
            held_since=T0 + 200,
        ),
    ]
    await harness.respawn(T0 + 1100)
    assert harness.observer.snapshot().paths["ipv4"].last_release_at == T0 + 1080

    await harness.round(T0 + 1140)
    # Only the idle one is pulled forward; the processing one waits.
    assert harness.scheduler.rescheduled == [((11,), T0 + 1140)]
    assert harness.payload["recheck"]["pending"] is True

    harness.scheduler.held = []
    await harness.round(T0 + 1200)
    assert harness.payload["recheck"]["pending"] is False


@pytest.mark.anyio
async def test_restart_after_a_long_disable_drops_the_stale_outage() -> None:
    """Plan section 7.6: an active phase is restored only when it is fresh."""
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    assert harness.payload["phase"] == PHASE_ACTIVE

    # Three probe intervals later the persisted observation is stale.
    await harness.respawn(T0 + 120 + 3 * 60 + 1)
    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.incident_id == 0
    assert snapshot.paths["ipv4"].state == PathState.UP
    assert snapshot.observed_at == T0 + 120
    assert snapshot.is_fresh(harness.clock["now"], 180) is False


@pytest.mark.anyio
async def test_restart_after_every_single_write_is_consistent() -> None:
    """A crash between any write and its send is recoverable from the row."""
    harness = Harness()
    harness.notifier._deliver = False
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.rounds(T0 + 180, T0 + 1080)
    harness.probes.down_paths = set()
    await harness.round(T0 + 1140)
    await harness.rounds(T0 + 1200, T0 + 2040)

    writes = list(harness.store.writes)
    assert writes
    for payload in writes:
        replay_store = FakeIncidentStore()
        replay_store.rows[SITE_INCIDENT_KEY] = CollectorIncident(
            incident_key=SITE_INCIDENT_KEY,
            opened_at=T0,
            last_alert_at=0,
            alert_count=0,
            payload=copy.deepcopy(payload),
        )
        observer = SiteConnectivityObserver(
            harness.config,
            incident_store=replay_store,
            scheduler=FakeScheduler(),
            probe_runner=FakeProbeRunner(),
            clock=lambda: payload["observed_at"] + 10,
            notifier=FakeNotifier(),
        )
        await observer.restore()
        restored = observer.build_payload()
        assert restored["phase"] == payload["phase"]
        assert restored["incident_id"] == payload["incident_id"]
        assert restored["ongoing"] == payload["ongoing"]
        assert restored["summaries"] == payload["summaries"]
        assert restored["recheck"] == payload["recheck"]
        assert restored["paths"] == payload["paths"]


@pytest.mark.anyio
async def test_a_failed_restore_never_writes_over_the_persisted_lifecycle() -> None:
    """A read failure must not let an empty observer erase the row.

    Reproduction of the review finding: the fresh observer's lifecycle is
    empty, so a round that ran before the restore succeeded would persist an
    empty ``summaries`` list over the pending record and lose it.
    """
    harness = Harness()
    harness.notifier._deliver = False
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)  # ipv4 down, incident T0 + 120
    await harness.rounds(T0 + 180, T0 + 1260)
    harness.probes.down_paths = set()
    await harness.round(T0 + 1320)  # recovering, release at T0 + 2220
    await harness.rounds(T0 + 1380, T0 + 2220)  # released, closed, summary pending

    pending = harness.payload["summaries"]
    assert [record["incident_id"] for record in pending] == [T0 + 120]
    assert harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)  # attempted, undelivered

    # The restart cannot read the row ...
    harness.store.fail_reads = True
    observer = await harness.respawn(T0 + 2280)
    writes_before = len(harness.store.writes)
    sends_before = len(harness.notifier.sent)

    await harness.round(T0 + 2280)

    # ... so the round does nothing at all: no write, no send, stale snapshot.
    assert len(harness.store.writes) == writes_before
    assert len(harness.notifier.sent) == sends_before
    assert harness.payload["summaries"] == pending
    snapshot = observer.snapshot()
    assert snapshot is not None
    assert snapshot.observed_at == 0
    assert snapshot.is_fresh(T0 + 2280, 180) is False

    # The next round retries the restore and delivers the pending summary.
    harness.store.fail_reads = False
    harness.notifier._deliver = True
    await harness.round(T0 + 2340)

    summaries = harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)
    assert summaries[-1].at == T0 + 2340
    assert f"outage {T0 + 120} lasted" in summaries[-1].error_msg
    assert harness.payload["summaries"] == []


@pytest.mark.anyio
async def test_an_absent_row_counts_as_restored() -> None:
    """ "Nothing to load" is not "the read failed": the first round runs."""
    harness = Harness()
    await harness.respawn(T0 + 60)
    assert harness.store.rows == {}

    await harness.round(T0 + 60)

    assert harness.payload["observed_at"] == T0 + 60
    assert harness.payload["phase"] == PHASE_IDLE


@pytest.mark.anyio
async def test_store_failures_do_not_stop_the_state_machine() -> None:
    """Plan 7.5: persistence is retried next round; memory still advances."""
    harness = Harness()
    harness.store.fail_writes = True
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.DOWN
    assert harness.store.rows == {}

    harness.store.fail_writes = False
    await harness.round(T0 + 180)
    assert harness.payload["phase"] == PHASE_ACTIVE
    assert harness.payload["incident_id"] == T0 + 120


@pytest.mark.anyio
async def test_a_failed_intent_write_prevents_the_send() -> None:
    """The intent must be durable before any I/O."""
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    harness.store.fail_writes = True
    await harness.rounds(T0 + 180, T0 + 1080)
    assert harness.notifier.sent == []

    harness.store.fail_writes = False
    await harness.round(T0 + 1140)
    assert len(harness.notifier.of_type(SITE_ONGOING_ERROR_TYPE)) == 1


@pytest.mark.anyio
async def test_a_raising_notifier_counts_as_undelivered() -> None:
    harness = Harness()
    harness.notifier.raises = True
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    await harness.round(T0 + 1020)

    assert len(harness.notifier.sent) == 1
    assert harness.payload["ongoing"]["delivered"] is False


@pytest.mark.anyio
async def test_the_site_incident_never_claims_an_alert() -> None:
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    await harness.rounds(T0 + 60, T0 + 1200)
    assert harness.store.claims == 0


# ---------------------------------------------------------------- recheck


def _held(check_id: int, dependency: Any, **overrides: Any) -> FakeHeldCheck:
    values: dict[str, Any] = {
        "next_check_time": T0 + 9999,
        "held_since": T0 + 100,
    }
    values.update(overrides)
    return FakeHeldCheck(
        check_id=check_id, data={"site_dependency": dependency}, **values
    )


async def _open_recheck(harness: Harness) -> int:
    """Drive one path down and back up so a pending recheck item exists.

    A pre-release claim is in flight during the closing round, which is what
    keeps the work item open past the round that created it.
    """
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    harness.probes.down_paths = set()
    await harness.round(T0 + 180)
    harness.scheduler.in_flight = 1
    await harness.round(T0 + 1080)
    harness.scheduler.in_flight = 0
    harness.scheduler.calls.clear()
    return T0 + 1080


@pytest.mark.anyio
async def test_recheck_reads_the_in_flight_count_before_the_held_set() -> None:
    """Plan 7.4 ordering invariant, with a result committed between the reads."""
    harness = Harness()
    release_at = await _open_recheck(harness)
    assert harness.payload["recheck"] == {"pending": True, "release_at": release_at}

    def commit_between_the_reads() -> None:
        harness.scheduler.in_flight = 0
        harness.scheduler.held = [_held(21, "internet")]

    harness.scheduler.in_flight = 1
    harness.scheduler.on_count = commit_between_the_reads

    await harness.round(T0 + 1140)

    assert harness.scheduler.calls == ["in_flight", "held"]
    # The claim was counted, so the item stays pending, and the check that the
    # commit created is visible to the held read and rescheduled.
    assert harness.payload["recheck"]["pending"] is True
    assert harness.scheduler.rescheduled == [((21,), T0 + 1140)]


@pytest.mark.anyio
async def test_recheck_stays_pending_while_a_pre_release_claim_is_in_flight() -> None:
    harness = Harness()
    await _open_recheck(harness)
    harness.scheduler.in_flight = 1

    await harness.round(T0 + 1140)
    assert harness.payload["recheck"]["pending"] is True
    assert harness.scheduler.rescheduled == []

    harness.scheduler.in_flight = 0
    await harness.round(T0 + 1200)
    assert harness.payload["recheck"]["pending"] is False


@pytest.mark.anyio
async def test_recheck_skips_checks_blocked_by_another_requirement() -> None:
    """Staggered release: only fully usable dependencies are pulled forward."""
    harness = Harness()
    harness.probes.down_paths = {"dns", "ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    harness.probes.down_paths = {"dns"}
    await harness.round(T0 + 180)  # ipv4 recovering, release at T0 + 1080

    harness.scheduler.held = [
        _held(31, "internet"),
        _held(32, {"requires": ["ipv4"]}),
        _held(33, "none"),
    ]
    await harness.round(T0 + 1080)  # ipv4 released, dns still down
    assert harness.payload["phase"] == PHASE_ACTIVE
    assert harness.payload["recheck"] == {"pending": True, "release_at": T0 + 1080}
    # Only the ipv4-only check is usable; the internet check still needs dns
    # and the unclassified one is not a dependent at all.
    assert harness.scheduler.rescheduled == [((32,), T0 + 1080)]

    harness.probes.down_paths = set()
    await harness.round(T0 + 1140)  # dns recovering, release at T0 + 2040
    harness.scheduler.held = [_held(31, "internet")]
    await harness.round(T0 + 2040)
    assert harness.scheduler.rescheduled[-1] == ((31,), T0 + 2040)
    assert harness.payload["recheck"]["release_at"] == T0 + 2040


@pytest.mark.anyio
async def test_recheck_filters_disabled_and_already_due_checks() -> None:
    harness = Harness()
    release_at = await _open_recheck(harness)
    harness.scheduler.held = [
        _held(41, "internet", disabled=True),
        _held(42, "internet", next_check_time=release_at - 10),
        _held(43, "internet", status="processing"),
        _held(44, "internet"),
    ]

    await harness.round(T0 + 1140)

    assert harness.scheduler.rescheduled == [((44,), T0 + 1140)]
    # All four remain usable dependents, so the item stays pending.
    assert harness.payload["recheck"]["pending"] is True


@pytest.mark.anyio
async def test_a_failed_reschedule_is_retried_next_round() -> None:
    harness = Harness()
    await _open_recheck(harness)
    harness.scheduler.held = [_held(51, "internet")]
    harness.scheduler.fail_reschedule = True

    await harness.round(T0 + 1140)
    assert harness.scheduler.rescheduled == []
    assert harness.payload["recheck"]["pending"] is True

    harness.scheduler.fail_reschedule = False
    await harness.round(T0 + 1200)
    assert harness.scheduler.rescheduled == [((51,), T0 + 1200)]


@pytest.mark.anyio
async def test_observe_mode_does_not_reschedule_anything() -> None:
    harness = Harness(make_config(mode=SiteMode.OBSERVE))
    await _open_recheck(harness)
    harness.scheduler.held = [_held(61, "internet")]

    await harness.round(T0 + 1140)

    assert harness.scheduler.calls == []
    assert harness.scheduler.rescheduled == []
    # Observe mode still runs the lifecycle and would send site messages.
    assert harness.payload["recheck"]["pending"] is True


@pytest.mark.anyio
async def test_off_mode_does_nothing() -> None:
    harness = Harness(make_config(mode=SiteMode.OFF))
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)

    assert harness.store.rows == {}
    assert harness.probes.probed == []
    assert harness.notifier.sent == []


# --------------------------------------------------------- hold decisions


def test_hold_reason_reports_a_confirmed_outage() -> None:
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.DOWN), down_since=T0 + 120),
        ipv6=PathSnapshot(state=str(PathState.DOWN), down_since=T0 + 180),
        incident_id=T0 + 120,
        observed_at=T0 + 300,
    )
    reason = snapshot.hold_reason(INTERNET, T0 + 300, T0 + 300, stale_after=180)
    assert reason == {
        "reason": REASON_DEPENDENCY_DOWN,
        "paths": ["ipv4", "ipv6"],
        "down_since": T0 + 120,
        "state": "down",
        "incident_id": T0 + 120,
    }


def test_hold_reason_reports_the_grace() -> None:
    snapshot = snapshot_of(
        ipv6=PathSnapshot(
            state=str(PathState.RECOVERING),
            down_since=T0 + 100,
            recovered_at=T0 + 200,
            release_at=T0 + 1100,
        ),
        incident_id=T0 + 100,
        observed_at=T0 + 300,
    )
    reason = snapshot.hold_reason(IPV6_ONLY, T0 + 300, T0 + 300, stale_after=180)
    assert reason is not None
    assert reason["reason"] == REASON_DEPENDENCY_RECOVERING
    assert reason["state"] == "recovering"
    assert reason["paths"] == ["ipv6"]


def test_hold_reason_reports_a_sample_measured_before_the_release() -> None:
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.UP), last_release_at=T0 + 1080),
        observed_at=T0 + 1100,
    )
    reason = snapshot.hold_reason(IPV4_ONLY, T0 + 1000, T0 + 1100, stale_after=180)
    assert reason is not None
    assert reason["reason"] == REASON_MEASURED_BEFORE_RELEASE
    assert reason["paths"] == ["ipv4"]
    assert reason["release_at"] == T0 + 1080

    # A sample claimed at or after the release is fresh.
    assert (
        snapshot.hold_reason(IPV4_ONLY, T0 + 1080, T0 + 1100, stale_after=180) is None
    )


def test_hold_reason_ignores_an_unknown_claim_time() -> None:
    """A permanent watermark must not hold a sample forever."""
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.UP), last_release_at=T0 + 1080),
        observed_at=T0 + 1100,
    )
    assert snapshot.hold_reason(IPV4_ONLY, 0, T0 + 1100, stale_after=180) is None


def test_an_any_of_group_that_never_lost_a_member_is_never_stale() -> None:
    """Freshness is per requirement, not per path.

    Reproduction of the review finding: after an IPv6-only outage the released
    ``ipv6`` watermark held every buffered ``internet`` failure, although the
    ``["ipv4", "ipv6"]`` requirement kept working over IPv4 throughout.
    """
    snapshot = snapshot_of(
        ipv6=PathSnapshot(state=str(PathState.UP), last_release_at=T0 + 1080),
        observed_at=T0 + 1100,
    )
    assert snapshot.hold_reason(INTERNET, T0 + 1000, T0 + 1100, stale_after=180) is None
    assert (
        snapshot.dependency_recovered(INTERNET, T0 + 1000, T0 + 1100, stale_after=180)
        is True
    )
    # The single-path requirement is still held: it really was unmet.
    assert (
        snapshot.hold_reason(IPV6_ONLY, T0 + 1000, T0 + 1100, stale_after=180)
        is not None
    )


def test_a_staggered_release_makes_a_group_fresh_from_its_first_member() -> None:
    """The group became usable when its first member was released."""
    first = T0 + 1000
    second = T0 + 2000
    now = second + 10
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.UP), last_release_at=first),
        ipv6=PathSnapshot(state=str(PathState.UP), last_release_at=second),
        observed_at=now,
    )

    # Claimed at or after the first release: IPv4 already carried the check.
    assert snapshot.hold_reason(INTERNET, first, now, stale_after=180) is None
    assert snapshot.hold_reason(INTERNET, first + 1, now, stale_after=180) is None
    assert (
        snapshot.dependency_recovered(INTERNET, first + 1, now, stale_after=180) is True
    )

    # Claimed before it: neither family worked, so the sample is stale.
    reason = snapshot.hold_reason(INTERNET, first - 1, now, stale_after=180)
    assert reason is not None
    assert reason["reason"] == REASON_MEASURED_BEFORE_RELEASE
    assert reason["paths"] == ["ipv4", "ipv6"]
    assert reason["release_at"] == first
    assert (
        snapshot.dependency_recovered(INTERNET, first - 1, now, stale_after=180)
        is False
    )

    # A single-path requirement keeps its own watermark.
    held = snapshot.hold_reason(IPV6_ONLY, second - 1, now, stale_after=180)
    assert held is not None
    assert held["reason"] == REASON_MEASURED_BEFORE_RELEASE
    assert held["paths"] == ["ipv6"]
    assert held["release_at"] == second
    assert snapshot.hold_reason(IPV6_ONLY, second, now, stale_after=180) is None


def test_a_group_is_stale_until_the_member_that_carries_it_was_released() -> None:
    """Reproduction of the review finding: a member still down contributes nothing.

    Both families were down; IPv4 was released while IPv6 stayed down. The
    ``["ipv4", "ipv6"]`` group is usable again only since the IPv4 release, so
    a sample claimed before it was measured while nothing carried the group.
    """
    release = T0 + 1020
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.UP), last_release_at=release),
        ipv6=PathSnapshot(state=str(PathState.DOWN), down_since=T0 + 120),
        observed_at=release + 40,
        incident_id=T0 + 120,
    )
    now = release + 40

    reason = snapshot.hold_reason(INTERNET, T0 + 1000, now, stale_after=180)
    assert reason is not None
    assert reason["reason"] == REASON_MEASURED_BEFORE_RELEASE
    assert reason["paths"] == ["ipv4"]
    assert reason["release_at"] == release
    assert (
        snapshot.dependency_recovered(INTERNET, T0 + 1000, now, stale_after=180)
        is False
    )

    # A sample claimed at or after that release is fresh.
    assert snapshot.hold_reason(INTERNET, release, now, stale_after=180) is None
    assert (
        snapshot.dependency_recovered(INTERNET, release, now, stale_after=180) is True
    )


def test_a_recovering_member_does_not_make_a_group_look_continuously_usable() -> None:
    """``recovering`` blocks its own dependents, so it carries no group either."""
    release = T0 + 1020
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.UP), last_release_at=release),
        ipv6=PathSnapshot(
            state=str(PathState.RECOVERING),
            down_since=T0 + 120,
            recovered_at=T0 + 900,
            release_at=T0 + 1800,
        ),
        observed_at=release + 40,
    )

    reason = snapshot.hold_reason(INTERNET, T0 + 1000, release + 40, stale_after=180)
    assert reason is not None
    assert reason["reason"] == REASON_MEASURED_BEFORE_RELEASE
    assert reason["paths"] == ["ipv4"]
    assert reason["release_at"] == release


def test_hold_reason_is_none_for_a_stale_snapshot() -> None:
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
        ipv6=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
        observed_at=T0,
    )
    assert snapshot.hold_reason(INTERNET, T0 + 500, T0 + 181, stale_after=180) is None
    assert (
        snapshot.hold_reason(INTERNET, T0 + 500, T0 + 180, stale_after=180) is not None
    )


def test_hold_reason_is_none_outside_enforce_mode() -> None:
    for mode in (SiteMode.OFF, SiteMode.OBSERVE):
        snapshot = snapshot_of(
            ipv4=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
            ipv6=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
            mode=mode,
        )
        assert snapshot.hold_reason(INTERNET, T0 + 5, T0 + 5, stale_after=180) is None
        # observed_reason ignores the mode, which is what observe mode records.
        assert (
            snapshot.observed_reason(INTERNET, T0 + 5, T0 + 5, stale_after=180)
            is not None
        )


def test_hold_reason_is_none_for_an_unclassified_check() -> None:
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
        ipv6=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
    )
    assert snapshot.hold_reason(None, T0 + 5, T0 + 5, stale_after=180) is None
    assert snapshot.dependency_recovered(None, T0 + 5, T0 + 5, stale_after=180) is False
    assert snapshot.dependency_usable(None) is False


def test_any_of_group_is_met_while_one_member_works() -> None:
    """Happy Eyeballs: an internet check is not held during an IPv6-only outage."""
    snapshot = snapshot_of(
        ipv6=PathSnapshot(state=str(PathState.DOWN), down_since=T0), incident_id=T0
    )
    assert snapshot.hold_reason(INTERNET, T0 + 5, T0 + 5, stale_after=180) is None
    assert snapshot.hold_reason(IPV6_ONLY, T0 + 5, T0 + 5, stale_after=180) is not None


def test_any_of_group_treats_failing_as_usable() -> None:
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.FAILING)),
        ipv6=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
    )
    assert snapshot.hold_reason(INTERNET, T0 + 5, T0 + 5, stale_after=180) is None
    assert snapshot.hold_reason(IPV4_ONLY, T0 + 5, T0 + 5, stale_after=180) is None


def test_ipv4_only_site_holds_internet_when_ipv4_is_down() -> None:
    """An unobserved member is ignored, so the group behaves like ``ipv4``."""
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.DOWN), down_since=T0),
        ipv6=PathSnapshot(state=str(PathState.UNOBSERVED)),
        incident_id=T0,
    )
    reason = snapshot.hold_reason(INTERNET, T0 + 5, T0 + 5, stale_after=180)
    assert reason is not None
    assert reason["paths"] == ["ipv4"]

    failing = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.FAILING)),
        ipv6=PathSnapshot(state=str(PathState.UNOBSERVED)),
    )
    assert failing.hold_reason(INTERNET, T0 + 5, T0 + 5, stale_after=180) is None


def test_a_requirement_on_an_unobserved_path_never_holds() -> None:
    snapshot = snapshot_of(ipv6=PathSnapshot(state=str(PathState.UNOBSERVED)))
    assert snapshot.hold_reason(IPV6_ONLY, T0 + 5, T0 + 5, stale_after=180) is None
    assert snapshot.dependency_usable(IPV6_ONLY) is True


def test_dependency_recovered_requires_a_fresh_snapshot_and_a_fresh_sample() -> None:
    snapshot = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.UP), last_release_at=T0 + 1080),
        observed_at=T0 + 1100,
    )
    assert (
        snapshot.dependency_recovered(IPV4_ONLY, T0 + 1000, T0 + 1100, stale_after=180)
        is False
    )
    assert (
        snapshot.dependency_recovered(IPV4_ONLY, T0 + 1080, T0 + 1100, stale_after=180)
        is True
    )
    stale = snapshot_of(
        ipv4=PathSnapshot(state=str(PathState.UP)), observed_at=T0 - 10_000
    )
    assert (
        stale.dependency_recovered(IPV4_ONLY, T0 + 1080, T0 + 1100, stale_after=180)
        is False
    )


def test_warn_unobserved_requirements_warns_once_per_check(caplog) -> None:
    check = FakeHeldCheck(check_id=77, data={"site_dependency": {"requires": ["ipv6"]}})
    other = FakeHeldCheck(check_id=78, data={"site_dependency": "internet"})
    with caplog.at_level("WARNING"):
        warn_unobserved_requirements([check, other], ["ipv6"])
        warn_unobserved_requirements([check, other], ["ipv6"])
    warnings = [record for record in caplog.records if "check_id=77" in record.message]
    assert len(warnings) == 1
    assert "check_id=78" not in caplog.text


@pytest.mark.anyio
async def test_observer_reports_its_unobserved_paths() -> None:
    harness = Harness(make_config(ipv6_targets=()))
    assert harness.observer.unobserved_paths() == ("ipv6",)
    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv6"].state == PathState.UNOBSERVED
    harness.observer.warn_unobserved_requirements(
        [FakeHeldCheck(check_id=81, data={"site_dependency": {"requires": ["ipv6"]}})]
    )


def test_null_site_state_never_holds() -> None:
    from nyxmon.adapters.site_connectivity import NullSiteState

    assert NullSiteState().snapshot() is None


# ------------------------------------------------------------- from_env


def test_from_env_defaults() -> None:
    config = SiteConnectivityConfig.from_env({})
    assert config.mode is SiteMode.OFF
    assert config.probe_interval == 60
    assert config.probe_timeout == 3
    assert config.down_after_failures == 2
    assert config.recovery_grace_seconds == 900
    assert config.max_hold_seconds == 10800
    assert config.incident_notify_after_seconds == 900
    assert config.incident_reminder_seconds == 21600
    assert config.stale_after_seconds == 180
    assert [target.host for target in config.ipv4_targets] == [
        "1.1.1.1",
        "8.8.8.8",
        "9.9.9.9",
    ]
    assert [target.host for target in config.ipv6_targets] == [
        "2606:4700:4700::1111",
        "2001:4860:4860::8888",
        "2620:fe::fe",
    ]
    assert [target.host for target in config.dns_names] == [
        "cloudflare.com",
        "google.com",
        "quad9.net",
    ]


def test_from_env_reads_every_knob() -> None:
    config = SiteConnectivityConfig.from_env(
        {
            "NYXMON_SITE_CONNECTIVITY_MODE": "Enforce",
            "NYXMON_SITE_PROBE_INTERVAL_SECONDS": "30",
            "NYXMON_SITE_PROBE_TIMEOUT_SECONDS": "5",
            "NYXMON_SITE_PROBE_IPV4_TARGETS": "9.9.9.9:53",
            "NYXMON_SITE_PROBE_IPV6_TARGETS": "[2620:fe::fe]:53",
            "NYXMON_SITE_PROBE_DNS_NAMES": "example.test",
            "NYXMON_SITE_DOWN_AFTER_FAILURES": "3",
            "NYXMON_SITE_RECOVERY_GRACE_SECONDS": "300",
            "NYXMON_SITE_MAX_HOLD_SECONDS": "3600",
            "NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS": "600",
            "NYXMON_SITE_INCIDENT_REMINDER_SECONDS": "7200",
        }
    )
    assert config.mode is SiteMode.ENFORCE
    assert config.probe_interval == 30
    assert config.stale_after_seconds == 90
    assert config.probe_timeout == 5
    assert config.ipv4_targets == (
        ProbeTarget(path="ipv4", kind="tcp", host="9.9.9.9", port=53),
    )
    assert config.ipv6_targets == (
        ProbeTarget(path="ipv6", kind="tcp", host="2620:fe::fe", port=53),
    )
    assert config.dns_names == (
        ProbeTarget(path="dns", kind="dns", host="example.test"),
    )
    assert config.down_after_failures == 3
    assert config.recovery_grace_seconds == 300
    assert config.max_hold_seconds == 3600
    assert config.incident_notify_after_seconds == 600
    assert config.incident_reminder_seconds == 7200


def test_from_env_empty_target_list_marks_a_path_unobserved() -> None:
    config = SiteConnectivityConfig.from_env({"NYXMON_SITE_PROBE_IPV6_TARGETS": ""})
    assert config.ipv6_targets == ()
    assert config.targets_for("ipv6") == ()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NYXMON_SITE_CONNECTIVITY_MODE", "loud"),
        ("NYXMON_SITE_PROBE_INTERVAL_SECONDS", "5"),
        ("NYXMON_SITE_PROBE_INTERVAL_SECONDS", "601"),
        ("NYXMON_SITE_PROBE_INTERVAL_SECONDS", "sixty"),
        ("NYXMON_SITE_PROBE_TIMEOUT_SECONDS", "0"),
        ("NYXMON_SITE_DOWN_AFTER_FAILURES", "11"),
        ("NYXMON_SITE_RECOVERY_GRACE_SECONDS", "59"),
        ("NYXMON_SITE_MAX_HOLD_SECONDS", "599"),
        ("NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS", "59"),
        ("NYXMON_SITE_INCIDENT_REMINDER_SECONDS", "2592001"),
        ("NYXMON_SITE_PROBE_IPV4_TARGETS", "1.1.1.1"),
        ("NYXMON_SITE_PROBE_IPV4_TARGETS", "2606:4700:4700::1111:443"),
        ("NYXMON_SITE_PROBE_IPV6_TARGETS", "8.8.8.8:443"),
        ("NYXMON_SITE_PROBE_IPV4_TARGETS", "1.1.1.1:0"),
        ("NYXMON_SITE_PROBE_DNS_NAMES", "not a name"),
    ],
)
def test_from_env_invalid_values_warn_once_and_use_the_default(
    name: str, value: str, caplog
) -> None:
    with caplog.at_level("WARNING"):
        first = SiteConnectivityConfig.from_env({name: value})
        second = SiteConnectivityConfig.from_env({name: value})
    default = SiteConnectivityConfig.from_env({})
    assert first == default
    assert second == default
    assert len([record for record in caplog.records if name in record.message]) == 1


# ------------------------------------------------------------------- run


@pytest.mark.anyio
async def test_run_restores_then_loops_until_stopped(monkeypatch) -> None:
    harness = Harness()
    harness.probes.down_paths = {"ipv4"}
    rounds: list[int] = []

    async def fake_wait() -> None:
        rounds.append(harness.clock["now"])
        harness.clock["now"] += 60
        if len(rounds) >= 3:
            harness.observer.stop()

    monkeypatch.setattr(harness.observer, "_wait_for_next_round", fake_wait)
    harness.clock["now"] = T0 + 60
    await harness.observer.run()

    assert len(rounds) == 3
    assert harness.payload["phase"] == PHASE_ACTIVE
    assert harness.payload["incident_id"] == T0 + 120


@pytest.mark.anyio
async def test_run_logs_and_continues_when_a_round_raises(monkeypatch) -> None:
    harness = Harness()
    calls = {"n": 0}

    async def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("probe subsystem is on fire")

    async def fake_wait() -> None:
        if calls["n"] >= 2:
            harness.observer.stop()

    monkeypatch.setattr(harness.observer, "run_once", boom)
    monkeypatch.setattr(harness.observer, "_wait_for_next_round", fake_wait)
    await harness.observer.run()

    assert calls["n"] == 2


@pytest.mark.anyio
async def test_run_returns_immediately_when_the_feature_is_off() -> None:
    harness = Harness(make_config(mode=SiteMode.OFF))
    await harness.observer.run()
    assert harness.store.rows == {}


@pytest.mark.anyio
async def test_resolve_site_dependency_accepts_a_held_row() -> None:
    row = FakeHeldCheck(check_id=1, data={"site_dependency": "internet"})
    assert resolve_site_dependency(row) == INTERNET


@pytest.mark.anyio
async def test_ipv4_only_site_holds_internet_checks_end_to_end() -> None:
    """No IPv6 targets: a confirmed IPv4 outage still holds ``internet``."""
    harness = Harness(make_config(ipv6_targets=()))
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv6"].state == PathState.UNOBSERVED
    reason = snapshot.hold_reason(INTERNET, T0 + 130, T0 + 130, stale_after=180)
    assert reason is not None
    assert reason["paths"] == ["ipv4"]
    assert reason["incident_id"] == T0 + 120
    # The unobserved path is not persisted; it carries no information.
    assert set(harness.payload["paths"]) == {"dns", "ipv4"}


@pytest.mark.anyio
async def test_a_raising_probe_counts_as_a_failed_target() -> None:
    harness = Harness()

    async def exploding_probe(target: ProbeTarget) -> bool:
        if target.path == "dns":
            raise RuntimeError("resolver segfaulted")
        return True

    harness.probes.probe = exploding_probe  # type: ignore[assignment]
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)

    snapshot = harness.observer.snapshot()
    assert snapshot is not None
    assert snapshot.paths["dns"].state == PathState.DOWN
    assert snapshot.paths["ipv4"].state == PathState.UP


@pytest.mark.anyio
async def test_lifecycle_against_the_real_in_memory_store() -> None:
    """The protocols this module declares are satisfied by the real store."""
    from nyxmon.adapters.repositories import InMemoryStore

    store = InMemoryStore()
    clock = {"now": T0}
    notifier = FakeNotifier(clock=clock)
    observer = SiteConnectivityObserver(
        make_config(),
        incident_store=store,
        scheduler=store.checks,
        probe_runner=FakeProbeRunner(),
        clock=lambda: clock["now"],
        notifier=notifier,
    )
    probes = observer._probe_runner
    assert isinstance(probes, FakeProbeRunner)
    probes.down_paths = {"ipv4"}
    for at in (T0 + 60, T0 + 120):
        clock["now"] = at
        await observer.run_once()

    incident = store.get_collector_incident(SITE_INCIDENT_KEY)
    assert incident is not None
    assert incident.payload["phase"] == PHASE_ACTIVE
    assert incident.payload["incident_id"] == T0 + 120

    # A fresh observer restores the row through the same protocol.
    restored = SiteConnectivityObserver(
        make_config(),
        incident_store=store,
        scheduler=store.checks,
        probe_runner=FakeProbeRunner(),
        clock=lambda: clock["now"],
        notifier=notifier,
    )
    await restored.restore()
    snapshot = restored.snapshot()
    assert snapshot is not None
    assert snapshot.paths["ipv4"].state == PathState.DOWN
    assert snapshot.incident_id == T0 + 120


@pytest.mark.anyio
async def test_default_probe_runner_reports_a_refused_connection() -> None:
    """A closed local port fails fast and never raises out of the runner."""
    from nyxmon.adapters.site_connectivity import DefaultProbeRunner

    runner = DefaultProbeRunner(timeout=1)
    target = ProbeTarget(path="ipv4", kind="tcp", host="127.0.0.1", port=1)
    assert await runner.probe(target) is False
    assert str(target) == "127.0.0.1:1"


@pytest.mark.anyio
async def test_held_checks_are_counted_without_the_blocking_portal() -> None:
    """The async variant is preferred; the sync one enters the portal."""
    harness = Harness()
    harness.scheduler.held_at_close = 4
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    harness.probes.down_paths = set()
    await harness.round(T0 + 1140)
    await harness.round(T0 + 2040)

    assert harness.scheduler.count_held_calls == ["async"]
    assert harness.notifier.of_type(SITE_SUMMARY_ERROR_TYPE)


@pytest.mark.anyio
async def test_a_scheduler_without_the_async_counter_still_works() -> None:
    """A scheduler offering only the synchronous form is used off the loop."""
    harness = Harness()

    class SyncOnlyScheduler(FakeScheduler):
        count_held_checks_async = None  # type: ignore[assignment]

    harness.scheduler = SyncOnlyScheduler()
    harness.scheduler.held_at_close = 2
    harness.observer = harness._build()
    harness.probes.down_paths = {"ipv4"}
    await harness.round(T0 + 60)
    await harness.round(T0 + 120)
    harness.probes.down_paths = set()
    await harness.round(T0 + 1140)
    await harness.round(T0 + 2040)

    assert harness.scheduler.count_held_calls == ["sync"]
    assert harness.payload["summaries"] == []
    assert "2 dependent check(s) were held" in harness.notifier.sent[-1].error_msg
