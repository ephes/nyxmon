"""End-to-end site connectivity holds, through bootstrap and the message bus.

Everything here is driven by an injected clock and a scripted probe runner, so
no test sleeps and none depends on wall time. The wiring under test is the
production one: :func:`nyxmon.bootstrap.bootstrap` builds the observer, hands
it to the collector, and injects it into the result handler as ``site_state``.

The acceptance rows of ``docs/site-connectivity-plan.md`` section 15 that
concern the handler are pinned here: a short reconnect stays silent, a long
outage produces one summary and one genuine alert, dependency semantics
(including any-of groups and unobserved paths) decide what is held, an
unclassified failure keeps alerting and retries its delivery, a stale or
disabled observer holds nothing, and the hold is bounded by
``max_hold_seconds``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from nyxmon.adapters.collector import AsyncCheckCollector
from nyxmon.adapters.repositories import InMemoryStore, SqliteStore
from nyxmon.adapters.repositories.interface import (
    NotificationState,
    NotificationStateConflict,
)
from nyxmon.adapters.site_connectivity import (
    REASON_DEPENDENCY_DOWN,
    REASON_DEPENDENCY_RECOVERING,
    REASON_MEASURED_BEFORE_RELEASE,
    SITE_INCIDENT_KEY,
    SITE_ONGOING_ERROR_TYPE,
    SITE_SUMMARY_ERROR_TYPE,
    NullSiteState,
    PathState,
    ProbeTarget,
    SiteConnectivityConfig,
    SiteConnectivityObserver,
    SiteMode,
    reset_site_config_warning_state,
)
from nyxmon.bootstrap import bootstrap
from nyxmon.domain.commands import AddCheckResult
from nyxmon.domain.models import (
    Check,
    CheckResult,
    CheckStatus,
    CheckType,
    Result,
    ResultStatus,
)
from nyxmon.service_layer.handlers import (
    ENV_NOTIFY_DELIVERY_RETRY_SECONDS,
    SITE_CONNECTIVITY_DATA_KEY,
    _delivery_retry_from_value,
)
from nyxmon.service_layer.site_dependency import reset_dependency_warning_state

T0 = 1_700_000_000
GRACE = 900
NOTIFY_AFTER = 900
PROBE_INTERVAL = 60
STALE_AFTER = 3 * PROBE_INTERVAL
MAX_HOLD = 10800
RETRY = 300

IPV4_TARGETS = (
    ProbeTarget(path="ipv4", kind="tcp", host="1.1.1.1", port=443),
    ProbeTarget(path="ipv4", kind="tcp", host="8.8.8.8", port=443),
)
IPV6_TARGETS = (
    ProbeTarget(path="ipv6", kind="tcp", host="2606:4700:4700::1111", port=443),
)
DNS_TARGETS = (ProbeTarget(path="dns", kind="dns", host="cloudflare.com"),)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reset_site_config_warning_state()
    reset_dependency_warning_state()
    _delivery_retry_from_value.cache_clear()
    monkeypatch.delenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, raising=False)
    monkeypatch.delenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", raising=False)
    monkeypatch.setenv("NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", "86400")
    yield
    reset_site_config_warning_state()
    reset_dependency_warning_state()
    _delivery_retry_from_value.cache_clear()


# ------------------------------------------------------------------ fakes


class Clock:
    """Explicit, injectable clock shared by handler, collector and observer."""

    def __init__(self, start: int = T0) -> None:
        self.now = start

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> None:
        self.now += seconds

    def move_to(self, epoch: int) -> None:
        self.now = epoch


class ScriptedProbeRunner:
    """Fails whole paths, or single targets, on demand."""

    def __init__(self) -> None:
        self.down_paths: set[str] = set()
        self.down_targets: set[str] = set()

    async def probe(self, target: ProbeTarget) -> bool:
        if str(target) in self.down_targets:
            return False
        return target.path not in self.down_paths


class ScriptedNotifier:
    """One notifier for per-check alerts and for the site messages."""

    def __init__(self) -> None:
        self.calls: list[tuple[Check, Result]] = []
        self.deliver: bool | Callable[[Check, Result], bool] = True

    def notify_check_failed(self, check: Check, result: Result) -> bool:
        self.calls.append((check, copy.deepcopy(result)))
        deliver = self.deliver
        return deliver(check, result) if callable(deliver) else deliver

    def notify_service_status_changed(self, service: Any, status: str) -> None:
        del service, status

    @property
    def check_alerts(self) -> list[tuple[Check, Result]]:
        return [call for call in self.calls if call[0].check_id != 0]

    def alerts_for(self, check_id: int) -> list[Result]:
        return [result for check, result in self.calls if check.check_id == check_id]

    def site_messages(self, error_type: str) -> list[Result]:
        return [
            result
            for check, result in self.calls
            if check.check_id == 0 and result.data.get("error_type") == error_type
        ]


def make_config(**overrides: Any) -> SiteConnectivityConfig:
    values: dict[str, Any] = {
        "mode": SiteMode.ENFORCE,
        "probe_interval": PROBE_INTERVAL,
        "probe_timeout": 3,
        "ipv4_targets": IPV4_TARGETS,
        "ipv6_targets": IPV6_TARGETS,
        "dns_names": DNS_TARGETS,
        "down_after_failures": 2,
        "recovery_grace_seconds": GRACE,
        "max_hold_seconds": MAX_HOLD,
        "incident_notify_after_seconds": NOTIFY_AFTER,
        "incident_reminder_seconds": 21600,
    }
    values.update(overrides)
    return SiteConnectivityConfig(**values)


# --------------------------------------------------------------- harness


@dataclass
class Harness:
    """A wired bus, its observer, and helpers to claim and submit samples."""

    store: Any
    bus: Any
    collector: AsyncCheckCollector
    observer: SiteConnectivityObserver | None
    notifier: ScriptedNotifier
    probes: ScriptedProbeRunner
    clock: Clock
    config: SiteConnectivityConfig
    checks: dict[int, Check] = field(default_factory=dict)

    async def _persist(self, check: Check) -> None:
        add_async = getattr(self.store.checks, "_add_async", None)
        if add_async is not None:
            await add_async(check)
        else:
            self.store.checks.add(check)

    async def add_check(
        self,
        check_id: int,
        *,
        dependency: Any = None,
        interval: int = 300,
        policy: dict[str, Any] | None = None,
        due_in: int = 300,
    ) -> Check:
        data: dict[str, Any] = {}
        if dependency is not None:
            data["site_dependency"] = dependency
        if policy is not None:
            data["notification_policy"] = policy
        check = Check(
            check_id=check_id,
            service_id=1,
            name=f"check-{check_id}",
            check_type=CheckType.HTTP,
            url="https://example.test/health",
            check_interval=interval,
            next_check_time=self.clock.now + due_in,
            data=data,
        )
        self.checks[check_id] = check
        await self._persist(check)
        return check

    async def claim(self, check_id: int, at: int | None = None) -> int:
        """Mark the check as processing, as the collector's claim would."""
        check = self.checks[check_id]
        at = self.clock.now if at is None else at
        check.status = CheckStatus.PROCESSING
        check.processing_started_at = at
        check.claim_started_at = at
        await self._persist(check)
        return at

    async def submit(
        self,
        check_id: int,
        status: str,
        *,
        claimed_at: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> Result:
        """Claim the check, run it, and hand the result to the bus."""
        check = self.checks[check_id]
        claimed_at = await self.claim(check_id, claimed_at)

        executing = copy.deepcopy(check)
        executing.status = CheckStatus.IDLE
        executing.processing_started_at = 0
        executing.next_check_time = self.clock.now + executing.check_interval
        result = Result(check_id=check_id, status=status, data=dict(data or {}))
        self.bus.handle(
            AddCheckResult(check_result=CheckResult(check=executing, result=result))
        )

        check.status = CheckStatus.IDLE
        check.processing_started_at = 0
        check.claim_started_at = 0
        check.next_check_time = executing.next_check_time
        return result

    def state(self, check_id: int) -> NotificationState:
        return self.store.checks.get_notification_state(check_id)

    async def round(self, *, down: set[str] | None = None, advance: int = 0) -> None:
        """Run one probe round with the given paths failing."""
        if advance:
            self.clock.advance(advance)
        if down is not None:
            self.probes.down_paths = set(down)
        assert self.observer is not None
        await self.observer.run_once()

    async def rounds(self, count: int, *, down: set[str] | None = None) -> None:
        for index in range(count):
            await self.round(down=down, advance=PROBE_INTERVAL if index else 0)

    async def elapse(self, seconds: int, *, down: set[str] | None = None) -> None:
        """Advance the clock one probe interval at a time, probing as we go.

        Time only passes with the observer running, so the snapshot stays fresh
        exactly as it would in production. Jumping the clock without rounds is
        the *stale observer* scenario and has its own test.
        """
        remaining = seconds
        while remaining > 0:
            step = min(PROBE_INTERVAL, remaining)
            remaining -= step
            await self.round(down=down, advance=step)

    def path(self, name: str) -> Any:
        assert self.observer is not None
        snapshot = self.observer.snapshot()
        assert snapshot is not None
        return snapshot.paths[name]


def build(
    monkeypatch,
    *,
    store: Any = None,
    clock: Clock | None = None,
    notifier: ScriptedNotifier | None = None,
    probes: ScriptedProbeRunner | None = None,
    **config_overrides: Any,
) -> Harness:
    clock = clock or Clock()
    monkeypatch.setattr("nyxmon.service_layer.handlers.current_epoch", clock)
    monkeypatch.setattr("nyxmon.adapters.collector.current_epoch", clock)
    store = InMemoryStore() if store is None else store
    notifier = notifier or ScriptedNotifier()
    probes = probes or ScriptedProbeRunner()
    config = make_config(**config_overrides)
    collector = AsyncCheckCollector()
    bus = bootstrap(
        store=store,
        collector=collector,
        notifier=notifier,
        site_config=config,
        site_probe_runner=probes,
        site_clock=clock,
    )
    observer = collector.site_observer
    return Harness(
        store=store,
        bus=bus,
        collector=collector,
        observer=observer,  # type: ignore[arg-type]
        notifier=notifier,
        probes=probes,
        clock=clock,
        config=config,
    )


async def _take_path_down(harness: Harness, path: str) -> int:
    """Two failed rounds confirm a path down; returns ``down_since``."""
    await harness.round(down={path})
    await harness.round(down={path}, advance=PROBE_INTERVAL)
    assert harness.path(path).state == PathState.DOWN
    return int(harness.path(path).down_since)


# ------------------------------------------------------- short reconnect


@pytest.mark.anyio
async def test_short_reconnect_produces_no_alert_at_all(monkeypatch) -> None:
    """Plan 12: a three-minute reconnect ends with zero notifications."""
    harness = build(monkeypatch)
    await harness.add_check(1, dependency="internet")

    # A first failure while everything is still up: counted, below threshold.
    await harness.submit(1, ResultStatus.ERROR)
    assert harness.state(1).failure_count == 1
    assert harness.state(1).held_since == 0

    down_since = await _take_path_down(harness, "dns")

    # The next failing sample would reach the threshold, but it is held.
    harness.clock.advance(30)
    result = await harness.submit(1, ResultStatus.ERROR)
    metadata = result.data[SITE_CONNECTIVITY_DATA_KEY]
    assert metadata["held"] is True
    assert metadata["reason"] == REASON_DEPENDENCY_DOWN
    assert metadata["paths"] == ["dns"]
    assert metadata["down_since"] == down_since
    held_since = harness.state(1).held_since
    assert held_since == harness.clock.now
    assert harness.state(1).failure_count == 2
    assert harness.notifier.calls == []

    # Reconnect, then the full grace.
    await harness.round(down=set(), advance=PROBE_INTERVAL)
    assert harness.path("dns").state == PathState.RECOVERING
    harness.clock.move_to(harness.path("dns").release_at)
    await harness.round(down=set())
    assert harness.path("dns").state == PathState.UP

    # The rechecked sample is healthy: the whole incident is cleared silently.
    await harness.submit(1, ResultStatus.OK)
    assert harness.state(1) == NotificationState(
        attempt_seq=harness.state(1).attempt_seq
    )
    assert harness.state(1).held_since == 0
    assert harness.notifier.calls == []


@pytest.mark.anyio
async def test_one_failing_probe_target_holds_nothing(monkeypatch) -> None:
    """A single dead server is not evidence of an outage."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch)
    await harness.add_check(1, dependency={"requires": ["ipv4"]})
    harness.probes.down_targets = {"1.1.1.1:443"}

    await harness.rounds(3)
    assert harness.path("ipv4").state == PathState.UP

    result = await harness.submit(1, ResultStatus.ERROR)
    assert SITE_CONNECTIVITY_DATA_KEY not in result.data
    assert len(harness.notifier.alerts_for(1)) == 1


# --------------------------------------------------------- long outage


@pytest.mark.anyio
async def test_long_outage_yields_one_summary_and_one_genuine_alert(
    monkeypatch,
) -> None:
    """Plan 12: 40 minutes down, one service still broken afterwards."""
    harness = build(monkeypatch)
    broken = await harness.add_check(1, dependency="internet")
    recovered = await harness.add_check(2, dependency="internet")
    del broken, recovered

    # Telegram is unreachable through the same broken path.
    harness.notifier.deliver = False
    down_since = await _take_path_down(harness, "dns")

    # Both dependants fail repeatedly while the path is down: all held.
    for _ in range(4):
        await harness.elapse(300, down={"dns"})
        await harness.submit(1, ResultStatus.ERROR)
        await harness.submit(2, ResultStatus.ERROR)
    assert harness.notifier.check_alerts == []
    assert harness.state(1).failure_count == 4
    assert harness.state(2).held_since > 0

    # 15 minutes in, the observer attempted the ongoing alert; it kept failing.
    assert harness.clock.now - down_since >= NOTIFY_AFTER
    ongoing_attempts = len(harness.notifier.site_messages(SITE_ONGOING_ERROR_TYPE))
    assert ongoing_attempts >= 1

    # Reconnect: the undelivered ongoing alert is retired, never sent late.
    harness.notifier.deliver = True
    await harness.round(down=set(), advance=PROBE_INTERVAL)
    assert harness.path("dns").state == PathState.RECOVERING
    await harness.elapse(180, down=set())
    assert (
        len(harness.notifier.site_messages(SITE_ONGOING_ERROR_TYPE)) == ongoing_attempts
    ), "an obsolete outage alert was delivered after reconnect"

    # Release: the outage closes and exactly one summary is sent.
    harness.clock.move_to(harness.path("dns").release_at)
    await harness.round(down=set())
    summaries = harness.notifier.site_messages(SITE_SUMMARY_ERROR_TYPE)
    assert len(summaries) == 1
    assert summaries[0].data["opsgate_ticket"] is False
    assert summaries[0].status == ResultStatus.WARNING

    # Further rounds must not repeat it.
    await harness.rounds(2)
    assert len(harness.notifier.site_messages(SITE_SUMMARY_ERROR_TYPE)) == 1

    # The recovered service goes quiet, the broken one alerts exactly once.
    harness.clock.advance(10)
    await harness.submit(2, ResultStatus.OK)
    await harness.submit(1, ResultStatus.ERROR)
    assert harness.notifier.alerts_for(2) == []
    assert len(harness.notifier.alerts_for(1)) == 1
    assert harness.state(1).held_since == 0

    harness.clock.advance(300)
    await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 1


@pytest.mark.anyio
async def test_flap_during_the_grace_keeps_holding(monkeypatch) -> None:
    """``recovering`` -> ``down`` keeps the holds and needs one clean grace."""
    harness = build(monkeypatch)
    await harness.add_check(1, dependency="internet")
    await _take_path_down(harness, "dns")

    await harness.round(down=set(), advance=PROBE_INTERVAL)
    assert harness.path("dns").state == PathState.RECOVERING
    first_release = harness.path("dns").release_at

    # A sample measured during the grace is still held.
    result = await harness.submit(1, ResultStatus.ERROR)
    assert result.data[SITE_CONNECTIVITY_DATA_KEY]["reason"] == (
        REASON_DEPENDENCY_RECOVERING
    )

    # A failed round returns the path to ``down`` without re-confirmation.
    await harness.round(down={"dns"}, advance=PROBE_INTERVAL)
    assert harness.path("dns").state == PathState.DOWN

    # The old release time has passed, but the grace restarted, so holds go on.
    harness.clock.move_to(first_release + 1)
    await harness.round(down={"dns"})
    result = await harness.submit(1, ResultStatus.ERROR)
    assert result.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is True
    assert harness.notifier.check_alerts == []

    await harness.round(down=set(), advance=PROBE_INTERVAL)
    assert harness.path("dns").release_at > first_release


# --------------------------------------------------- dependency semantics


@pytest.mark.anyio
async def test_ipv6_only_outage_holds_ipv6_checks_but_not_internet(
    monkeypatch,
) -> None:
    """Happy Eyeballs keeps dual-stack checks working; they must still alert."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch)
    await harness.add_check(1, dependency="internet")
    await harness.add_check(2, dependency={"requires": ["ipv6"]})
    await _take_path_down(harness, "ipv6")

    internet = await harness.submit(1, ResultStatus.ERROR)
    ipv6_only = await harness.submit(2, ResultStatus.ERROR)

    assert SITE_CONNECTIVITY_DATA_KEY not in internet.data
    assert len(harness.notifier.alerts_for(1)) == 1
    assert ipv6_only.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is True
    assert ipv6_only.data[SITE_CONNECTIVITY_DATA_KEY]["paths"] == ["ipv6"]
    assert harness.notifier.alerts_for(2) == []


@pytest.mark.anyio
async def test_dns_outage_holds_internet_checks(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch)
    await harness.add_check(1, dependency="internet")
    await _take_path_down(harness, "dns")

    result = await harness.submit(1, ResultStatus.ERROR)

    assert result.data[SITE_CONNECTIVITY_DATA_KEY]["paths"] == ["dns"]
    assert harness.notifier.check_alerts == []


@pytest.mark.anyio
async def test_ipv4_only_site_holds_internet_checks_on_an_ipv4_outage(
    monkeypatch,
) -> None:
    """With no IPv6 targets, ``["ipv4", "ipv6"]`` behaves exactly like ``ipv4``."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch, ipv6_targets=())
    await harness.add_check(1, dependency="internet")
    assert harness.path("ipv6").state == PathState.UNOBSERVED

    # A single failed round only makes the path ``failing``: nothing is held.
    await harness.round(down={"ipv4"})
    assert harness.path("ipv4").state == PathState.FAILING
    failing_sample = await harness.submit(1, ResultStatus.ERROR)
    assert SITE_CONNECTIVITY_DATA_KEY not in failing_sample.data
    assert len(harness.notifier.alerts_for(1)) == 1

    # Confirmed down: now the any-of group is unmet and the check is held.
    await harness.round(down={"ipv4"}, advance=PROBE_INTERVAL)
    assert harness.path("ipv4").state == PathState.DOWN
    held = await harness.submit(1, ResultStatus.ERROR)
    assert held.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is True
    assert held.data[SITE_CONNECTIVITY_DATA_KEY]["paths"] == ["ipv4"]
    assert len(harness.notifier.alerts_for(1)) == 1


@pytest.mark.anyio
async def test_a_requirement_on_an_unobserved_path_never_holds_and_warns_once(
    monkeypatch, caplog
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch, ipv6_targets=())
    await harness.add_check(1, dependency={"requires": ["ipv6"]}, due_in=10_000)

    with caplog.at_level("WARNING"):
        await harness.collector._collect_once()
        await harness.collector._warn_unobserved_site_requirements()

    assert caplog.text.count("requires the unobserved path(s) ipv6") == 1

    await harness.rounds(3, down={"ipv4", "dns"})
    result = await harness.submit(1, ResultStatus.ERROR)

    assert SITE_CONNECTIVITY_DATA_KEY not in result.data
    assert len(harness.notifier.alerts_for(1)) == 1


@pytest.mark.anyio
async def test_malformed_site_dependency_warns_once_and_is_unclassified(
    monkeypatch, caplog
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch)
    await harness.add_check(1, dependency={"requires": ["wifi"]})
    await _take_path_down(harness, "dns")

    with caplog.at_level("WARNING"):
        first = await harness.submit(1, ResultStatus.ERROR)
        harness.clock.advance(300)
        second = await harness.submit(1, ResultStatus.ERROR)

    assert SITE_CONNECTIVITY_DATA_KEY not in first.data
    assert SITE_CONNECTIVITY_DATA_KEY not in second.data
    assert caplog.text.count("site_dependency for check_id=1 is invalid") == 1
    assert len(harness.notifier.alerts_for(1)) == 1


# ---------------------------------------- unclassified checks and retries


@pytest.mark.anyio
async def test_local_failure_during_an_outage_alerts_and_retries_delivery(
    monkeypatch,
) -> None:
    """Plan 12: a local disk failure keeps paging, and its send is retried."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    harness = build(monkeypatch)
    await harness.add_check(1)  # unclassified: no site_dependency
    await _take_path_down(harness, "dns")

    # Telegram is unreachable during the full outage.
    harness.notifier.deliver = False
    await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 1
    assert harness.state(1).attempt_at == harness.clock.now

    # Below the retry interval: no repeat.
    harness.clock.advance(RETRY - 1)
    await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 1

    # Every retry interval one further attempt, all failing.
    for _ in range(2):
        harness.clock.advance(RETRY)
        await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 3
    assert harness.state(1).attempt_at == harness.clock.now

    # After the reconnect the still-failing check finally delivers.
    harness.notifier.deliver = True
    harness.clock.advance(RETRY)
    await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 4
    assert harness.state(1).attempt_at == 0

    # Nothing repeats after the acknowledged send.
    harness.clock.advance(RETRY * 4)
    await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 4


@pytest.mark.anyio
async def test_a_local_failure_that_recovered_is_never_reported_late(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    harness = build(monkeypatch)
    await harness.add_check(1)
    harness.notifier.deliver = False

    await harness.submit(1, ResultStatus.ERROR)
    harness.notifier.deliver = True
    harness.clock.advance(RETRY + 1)
    await harness.submit(1, ResultStatus.OK)

    harness.clock.advance(RETRY * 5)
    assert len(harness.notifier.alerts_for(1)) == 1
    assert harness.state(1).attempt_at == 0


@pytest.mark.anyio
async def test_the_retry_knob_at_zero_keeps_todays_behaviour(monkeypatch) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch)
    await harness.add_check(1)
    harness.notifier.deliver = False

    await harness.submit(1, ResultStatus.ERROR)
    for _ in range(5):
        harness.clock.advance(RETRY * 2)
        await harness.submit(1, ResultStatus.ERROR)

    assert len(harness.notifier.alerts_for(1)) == 1


# --------------------------------------------------- freshness and recheck


@pytest.mark.anyio
async def test_a_sample_measured_before_the_release_is_held_and_rechecked(
    monkeypatch,
) -> None:
    """Plan 7.3/7.4: a buffered result must not alert after the reconnect."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch)
    await harness.add_check(1, dependency="internet", due_in=3000)
    await _take_path_down(harness, "dns")

    # The check is claimed during the grace and is still running at release ...
    await harness.round(down=set(), advance=PROBE_INTERVAL)
    claimed_at = await harness.claim(1)
    release_at = harness.path("dns").release_at

    # ... the release happens while that claim is still in flight, so the
    # recheck work item stays pending ...
    harness.clock.move_to(release_at)
    await harness.round(down=set())
    assert harness.path("dns").last_release_at == release_at

    # ... and only afterwards is the buffered failing result handled.
    harness.clock.advance(120)
    result = await harness.submit(1, ResultStatus.ERROR, claimed_at=claimed_at)
    metadata = result.data[SITE_CONNECTIVITY_DATA_KEY]
    assert metadata["held"] is True
    assert metadata["reason"] == REASON_MEASURED_BEFORE_RELEASE
    assert metadata["release_at"] == release_at
    assert harness.notifier.check_alerts == []

    # The next observer round pulls the held, idle check forward.
    await harness.round(down=set(), advance=PROBE_INTERVAL)
    assert harness.store.checks.get(1).next_check_time == harness.clock.now

    # Its fresh sample decides: it re-arms the budget and alerts on its merits.
    harness.clock.advance(10)
    fresh = await harness.submit(1, ResultStatus.ERROR)
    assert SITE_CONNECTIVITY_DATA_KEY not in fresh.data
    assert harness.state(1).held_since == 0
    assert len(harness.notifier.alerts_for(1)) == 1


@pytest.mark.anyio
async def test_held_samples_count_towards_the_checks_own_threshold(
    monkeypatch,
) -> None:
    """An hourly threshold-4 check still needs its remaining samples."""
    harness = build(monkeypatch)
    await harness.add_check(
        1,
        dependency="internet",
        interval=3600,
        policy={"consecutive_failures": 4},
    )
    await harness.add_check(
        2,
        dependency="internet",
        interval=300,
        policy={"consecutive_failures": 2},
    )
    await _take_path_down(harness, "dns")

    # One held sample for the hourly check, two for the five-minute one.
    await harness.submit(1, ResultStatus.ERROR)
    await harness.submit(2, ResultStatus.ERROR)
    await harness.elapse(300, down={"dns"})
    await harness.submit(2, ResultStatus.ERROR)
    assert harness.notifier.check_alerts == []

    # Reconnect and release.
    await harness.round(down=set(), advance=PROBE_INTERVAL)
    harness.clock.move_to(harness.path("dns").release_at)
    await harness.round(down=set())

    # The five-minute check already met its threshold: it alerts at once.
    harness.clock.advance(10)
    await harness.submit(2, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(2)) == 1

    # The hourly check needs three more samples, exactly as without the outage.
    for _ in range(2):
        harness.clock.advance(3600)
        await harness.submit(1, ResultStatus.ERROR)
    assert harness.notifier.alerts_for(1) == []
    harness.clock.advance(3600)
    await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 1


@pytest.mark.anyio
async def test_a_check_blocked_by_another_requirement_is_not_rescheduled(
    monkeypatch,
) -> None:
    """Staggered release: only checks whose complete dependency is usable move."""
    harness = build(monkeypatch)
    await harness.add_check(1, dependency="internet", interval=3600, due_in=3600)
    await harness.add_check(
        2, dependency={"requires": ["ipv4"]}, interval=3600, due_in=3600
    )

    await harness.rounds(2, down={"ipv4", "dns"})
    await harness.submit(1, ResultStatus.ERROR)
    await harness.submit(2, ResultStatus.ERROR)
    assert harness.state(1).held_since > 0
    assert harness.state(2).held_since > 0

    # ipv4 comes back and is released; dns is still down.
    await harness.round(down={"dns"}, advance=PROBE_INTERVAL)
    harness.clock.move_to(harness.path("ipv4").release_at)
    await harness.round(down={"dns"})

    assert harness.store.checks.get(2).next_check_time == harness.clock.now
    assert harness.store.checks.get(1).next_check_time > harness.clock.now


# ---------------------------------------------- observer failure modes


@pytest.mark.anyio
async def test_a_stale_snapshot_stops_holding_without_touching_held_since(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch)
    await harness.add_check(1, dependency="internet")
    await _take_path_down(harness, "dns")

    await harness.submit(1, ResultStatus.ERROR)
    held_since = harness.state(1).held_since
    assert held_since > 0
    assert harness.notifier.check_alerts == []

    # The observer freezes: no further rounds, the snapshot goes stale.
    harness.clock.advance(STALE_AFTER + 1)
    result = await harness.submit(1, ResultStatus.ERROR)

    assert SITE_CONNECTIVITY_DATA_KEY not in result.data
    assert len(harness.notifier.alerts_for(1)) == 1
    assert harness.state(1).held_since == held_since, (
        "a stale bypass must not re-arm the hold budget"
    )


@pytest.mark.anyio
async def test_the_hold_is_bounded_by_max_hold_seconds(monkeypatch) -> None:
    """Plan 12: a defective observer delays an alert at most ``max_hold``."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    monkeypatch.setenv(ENV_NOTIFY_DELIVERY_RETRY_SECONDS, str(RETRY))
    harness = build(monkeypatch, max_hold_seconds=1800)
    await harness.add_check(1, dependency="internet")
    await _take_path_down(harness, "dns")

    await harness.submit(1, ResultStatus.ERROR)
    held_since = harness.state(1).held_since
    assert harness.notifier.check_alerts == []

    # Still inside the budget, still down: held.
    harness.clock.advance(1700)
    await harness.round(down={"dns"})
    await harness.submit(1, ResultStatus.ERROR)
    assert harness.notifier.check_alerts == []

    # The budget is spent: ordinary policy applies although the path is down.
    harness.clock.advance(200)
    await harness.round(down={"dns"})
    harness.notifier.deliver = False
    exhausted = await harness.submit(1, ResultStatus.ERROR)
    assert exhausted.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is False
    assert exhausted.data[SITE_CONNECTIVITY_DATA_KEY]["exhausted"] is True
    assert len(harness.notifier.alerts_for(1)) == 1
    assert harness.state(1).held_since == held_since, "the budget must not re-arm"

    # The delivery retry after exhaustion is not held either.
    harness.notifier.deliver = True
    harness.clock.advance(RETRY)
    await harness.round(down={"dns"})
    await harness.submit(1, ResultStatus.ERROR)
    assert len(harness.notifier.alerts_for(1)) == 2
    assert harness.state(1).held_since == held_since


# ------------------------------------------------------------- the modes


@pytest.mark.anyio
async def test_observe_mode_records_metadata_without_changing_decisions(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch, mode=SiteMode.OBSERVE)
    await harness.add_check(1, dependency="internet")
    await _take_path_down(harness, "dns")

    result = await harness.submit(1, ResultStatus.ERROR)

    metadata = result.data[SITE_CONNECTIVITY_DATA_KEY]
    assert metadata["held"] is False
    assert metadata["reason"] == REASON_DEPENDENCY_DOWN
    assert len(harness.notifier.alerts_for(1)) == 1
    assert harness.state(1).held_since == 0


@pytest.mark.anyio
async def test_mode_off_starts_no_observer_and_writes_no_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    harness = build(monkeypatch, mode=SiteMode.OFF)

    assert harness.observer is None
    assert harness.collector.site_observer is None

    await harness.add_check(1, dependency="internet")
    result = await harness.submit(1, ResultStatus.ERROR)

    assert SITE_CONNECTIVITY_DATA_KEY not in result.data
    assert len(harness.notifier.alerts_for(1)) == 1
    assert harness.store.get_collector_incident(SITE_INCIDENT_KEY) is None


def test_null_site_state_never_holds() -> None:
    assert NullSiteState().snapshot() is None


# --------------------------------------------------------------- restarts


@pytest.mark.anyio
async def test_a_restart_during_the_grace_keeps_holding(monkeypatch, tmp_path) -> None:
    """A new process over the same database honours the persisted release."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    db_path = tmp_path / "nyxmon.sqlite3"
    clock = Clock()
    first = build(monkeypatch, store=SqliteStore(db_path), clock=clock)
    await first.add_check(1, dependency="internet")
    await _take_path_down(first, "dns")
    await first.submit(1, ResultStatus.ERROR)
    assert first.state(1).held_since > 0

    await first.round(down=set(), advance=PROBE_INTERVAL)
    release_at = first.path("dns").release_at
    assert first.path("dns").state == PathState.RECOVERING

    # --- restart: a brand new bootstrap over the same file ---
    second = build(
        monkeypatch,
        store=SqliteStore(db_path),
        clock=clock,
        notifier=ScriptedNotifier(),
    )
    second.checks = first.checks
    assert second.observer is not None
    await second.observer.restore()

    assert second.path("dns").state == PathState.RECOVERING
    assert second.path("dns").release_at == release_at

    # The grace is neither restarted nor released early: still held.
    clock.advance(30)
    result = await second.submit(1, ResultStatus.ERROR)
    assert result.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is True
    assert second.notifier.check_alerts == []

    # After the persisted release the fresh sample decides on its own merits.
    clock.move_to(release_at)
    await second.round(down=set())
    clock.advance(10)
    fresh = await second.submit(1, ResultStatus.ERROR)
    assert SITE_CONNECTIVITY_DATA_KEY not in fresh.data
    assert len(second.notifier.alerts_for(1)) == 1


@pytest.mark.anyio
async def test_a_restart_during_an_incident_restores_down_and_keeps_holding(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    db_path = tmp_path / "nyxmon.sqlite3"
    clock = Clock()
    first = build(monkeypatch, store=SqliteStore(db_path), clock=clock)
    await first.add_check(1, dependency="internet")
    down_since = await _take_path_down(first, "dns")

    second = build(
        monkeypatch,
        store=SqliteStore(db_path),
        clock=clock,
        notifier=ScriptedNotifier(),
    )
    second.checks = first.checks
    assert second.observer is not None
    await second.observer.restore()

    assert second.path("dns").state == PathState.DOWN
    assert second.path("dns").down_since == down_since

    clock.advance(30)
    result = await second.submit(1, ResultStatus.ERROR)
    assert result.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is True
    assert second.notifier.check_alerts == []


@pytest.mark.anyio
async def test_a_staggered_release_holds_a_buffered_sample_across_a_restart(
    monkeypatch, tmp_path
) -> None:
    """Reproduction of the review finding, end to end through the handler.

    Both families were down. IPv4 was released while IPv6 stayed down, so the
    ``["ipv4", "ipv6"]`` group of ``internet`` is usable again only since the
    IPv4 release. A sample claimed before it measured a dead internet and must
    be held, before and after a restart, until a fresh sample decides.
    """
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    db_path = tmp_path / "nyxmon.sqlite3"
    clock = Clock()
    first = build(monkeypatch, store=SqliteStore(db_path), clock=clock)
    await first.add_check(1, dependency="internet", due_in=3000)

    await first.rounds(2, down={"ipv4", "ipv6"})
    assert first.path("ipv4").state == PathState.DOWN
    assert first.path("ipv6").state == PathState.DOWN

    # IPv4 comes back and serves its grace; IPv6 stays down throughout.
    await first.round(down={"ipv6"}, advance=PROBE_INTERVAL)
    assert first.path("ipv4").state == PathState.RECOVERING
    release_at = int(first.path("ipv4").release_at)
    await first.elapse(release_at - clock.now - PROBE_INTERVAL, down={"ipv6"})

    # The check is claimed while nothing carries the group ...
    claimed_at = await first.claim(1)
    assert claimed_at < release_at

    # ... and the IPv4 release happens while that claim is still in flight.
    clock.move_to(release_at)
    await first.round(down={"ipv6"})
    assert first.path("ipv4").state == PathState.UP
    assert first.path("ipv4").last_release_at == release_at
    assert first.path("ipv6").state == PathState.DOWN

    clock.advance(120)
    result = await first.submit(1, ResultStatus.ERROR, claimed_at=claimed_at)
    metadata = result.data[SITE_CONNECTIVITY_DATA_KEY]
    assert metadata["held"] is True
    assert metadata["reason"] == REASON_MEASURED_BEFORE_RELEASE
    assert metadata["paths"] == ["ipv4"]
    assert metadata["release_at"] == release_at
    assert first.state(1).held_since > 0
    assert first.notifier.check_alerts == []

    # --- restart: a brand new bootstrap over the same file ---
    second = build(
        monkeypatch,
        store=SqliteStore(db_path),
        clock=clock,
        notifier=ScriptedNotifier(),
    )
    second.checks = first.checks
    assert second.observer is not None
    await second.observer.restore()
    assert second.path("ipv4").last_release_at == release_at
    assert second.path("ipv6").state == PathState.DOWN

    # The restored watermark still holds another buffered pre-release sample.
    clock.advance(30)
    buffered = await second.submit(1, ResultStatus.ERROR, claimed_at=claimed_at)
    assert buffered.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is True
    assert second.state(1).held_since > 0
    assert second.notifier.check_alerts == []

    # A sample claimed after the release decides on its own merits.
    clock.advance(10)
    fresh = await second.submit(1, ResultStatus.ERROR)
    assert SITE_CONNECTIVITY_DATA_KEY not in fresh.data
    assert second.state(1).held_since == 0
    assert len(second.notifier.alerts_for(1)) == 1


# -------------------------------------------- conflict exhaustion fallback


class _ConflictingStore(InMemoryStore):
    """Fails the compare-and-swap a fixed number of times."""

    conflicts: int = 0
    hold_markers: list[int | None] = []

    def persist_check_result(
        self,
        check: Check,
        result: Result,
        notification_transition: Any,
        *,
        complete_check: bool = True,
        hold_marker: int | None = None,
    ) -> bool:
        if notification_transition is not None and self.conflicts > 0:
            self.conflicts -= 1
            raise NotificationStateConflict(check.check_id)
        self.hold_markers.append(hold_marker)
        return super().persist_check_result(
            check,
            result,
            notification_transition,
            complete_check=complete_check,
            hold_marker=hold_marker,
        )


@pytest.mark.anyio
async def test_the_conflict_fallback_marks_the_hold_with_the_completion(
    monkeypatch,
) -> None:
    """Plan 7.2: the recheck obligation is atomic with the completion."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    store = _ConflictingStore()
    store.conflicts = 3
    store.hold_markers = []
    harness = build(monkeypatch, store=store)
    await harness.add_check(1, dependency="internet")
    await _take_path_down(harness, "dns")

    result = await harness.submit(1, ResultStatus.ERROR)

    assert store.hold_markers == [harness.clock.now]
    assert result.data[SITE_CONNECTIVITY_DATA_KEY]["held"] is True
    assert harness.state(1).held_since == harness.clock.now
    assert harness.notifier.check_alerts == []

    # The observer sees the check as owing a recheck, never idle-and-unheld.
    held = await store.checks.list_held_checks_async()
    assert [row.check_id for row in held] == [1]


@pytest.mark.anyio
async def test_the_conflict_fallback_passes_no_marker_for_an_unheld_sample(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    store = _ConflictingStore()
    store.conflicts = 3
    store.hold_markers = []
    harness = build(monkeypatch, store=store)
    await harness.add_check(1)  # unclassified

    await harness.submit(1, ResultStatus.ERROR)

    assert store.hold_markers == [None]
    assert harness.state(1).held_since == 0
    assert harness.notifier.check_alerts == []


@pytest.mark.anyio
async def test_a_stale_claim_marks_no_hold(monkeypatch, tmp_path) -> None:
    """A rejected completion must not leave a recheck obligation behind."""
    monkeypatch.setenv("NYXMON_NOTIFY_CONSECUTIVE_FAILURES", "1")
    db_path = tmp_path / "nyxmon.sqlite3"
    harness = build(monkeypatch, store=SqliteStore(db_path))
    check = await harness.add_check(1, dependency="internet")
    await _take_path_down(harness, "dns")

    # A newer worker holds the claim; the old result arrives afterwards.
    check.status = CheckStatus.PROCESSING
    check.processing_started_at = harness.clock.now
    await harness.store.checks._add_async(check)

    stale = copy.deepcopy(check)
    stale.claim_started_at = harness.clock.now - 5000
    stale.status = CheckStatus.IDLE
    stale.processing_started_at = 0
    result = Result(check_id=1, status=ResultStatus.ERROR, data={})
    harness.store.persist_check_result(
        stale, result, None, hold_marker=harness.clock.now
    )

    assert await harness.store.checks.count_held_checks_async() == 0
