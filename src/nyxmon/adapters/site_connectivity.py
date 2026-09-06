"""Site connectivity observation, incident coordination and recovery recheck.

One probe loop per process measures three connectivity paths (``dns``,
``ipv4``, ``ipv6``), keeps a small state machine per path, and publishes an
immutable snapshot that the result handler consults before it alerts. When a
provider reconnect breaks every internet-dependent check at once, dependent
alerts are held instead of paging, and the site outage itself is reported as a
single, deduplicated incident.

The whole lifecycle lives in **one** row of ``collector_incident`` under the
key ``site:connectivity``: a phase machine whose every transition is a single
atomic payload write. The row is never deleted, because it carries the
per-path release watermarks (``last_release_at``) that let the handler
recognise a sample measured before a recovery was released.

See ``docs/site-connectivity-plan.md`` sections 4-7 and 11 for the
specification this module implements.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, TypeAlias

import anyio
from anyio import to_thread

from ..service_layer.site_dependency import (
    PATH_NAMES,
    SiteDependency,
    resolve_site_dependency,
)
from .repositories.interface import CollectorIncident, CollectorIncidentAlert

logger = logging.getLogger(__name__)

__all__ = [
    "DefaultProbeRunner",
    "NullSiteState",
    "PATH_NAMES",
    "PathSnapshot",
    "PathState",
    "ProbeRunner",
    "ProbeTarget",
    "SITE_INCIDENT_KEY",
    "SITE_SUMMARY_MAX_CHARS",
    "SITE_SUMMARY_MAX_RECORDS",
    "SiteCheckScheduler",
    "SiteConnectivityConfig",
    "SiteConnectivityObserver",
    "SiteConnectivitySnapshot",
    "SiteIncidentStore",
    "SiteMode",
    "SiteNotifier",
    "SiteStateProvider",
    "reset_site_config_warning_state",
    "warn_unobserved_requirements",
]

# --------------------------------------------------------------- constants

#: The single collector incident row that carries the whole site lifecycle.
SITE_INCIDENT_KEY = "site:connectivity"

#: Human readable name used for the synthetic check row of site messages.
SITE_INCIDENT_NAME = "site connectivity"

#: ``payload["incident_type"]`` marker, mirroring the other collector incidents.
SITE_INCIDENT_TYPE = "site_connectivity"

#: Schema version of the payload written by this module.
SITE_PAYLOAD_VERSION = 1

#: Retry cadence for an undelivered site message (ongoing alert or summary).
SITE_DELIVERY_RETRY_SECONDS = 60

#: How many pending summary records one message may cover.
SITE_SUMMARY_MAX_RECORDS = 5

#: Character budget for the body of one summary message. Telegram rejects
#: anything above 4096 characters, so a backlog is drained in batches instead
#: of being concatenated into a message that can never be delivered.
SITE_SUMMARY_MAX_CHARS = 3500

SITE_ONGOING_ERROR_TYPE = "site_connectivity_outage"
SITE_SUMMARY_ERROR_TYPE = "site_connectivity_summary"

PHASE_IDLE = "idle"
PHASE_ACTIVE = "active"

REASON_DEPENDENCY_DOWN = "dependency_down"
REASON_DEPENDENCY_RECOVERING = "dependency_recovering"
REASON_MEASURED_BEFORE_RELEASE = "measured_before_release"

PROBE_KIND_TCP = "tcp"
PROBE_KIND_DNS = "dns"

#: Hard budget for one probe round on top of the per-target timeout.
PROBE_ROUND_SLACK_SECONDS = 2


class SiteMode(StrEnum):
    """How far the site connectivity feature is switched on."""

    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class PathState(StrEnum):
    """State of one observed connectivity path."""

    UP = "up"
    FAILING = "failing"
    DOWN = "down"
    RECOVERING = "recovering"
    UNOBSERVED = "unobserved"


#: States in which a path counts as usable by a dependent check. ``failing`` is
#: usable on purpose: an unconfirmed failure must never defer an alert.
USABLE_STATES = frozenset({PathState.UP, PathState.FAILING})

#: States in which a path blocks its dependents.
BLOCKING_STATES = frozenset({PathState.DOWN, PathState.RECOVERING})

# ------------------------------------------------------------- environment

ENV_MODE = "NYXMON_SITE_CONNECTIVITY_MODE"
ENV_PROBE_INTERVAL = "NYXMON_SITE_PROBE_INTERVAL_SECONDS"
ENV_PROBE_TIMEOUT = "NYXMON_SITE_PROBE_TIMEOUT_SECONDS"
ENV_IPV4_TARGETS = "NYXMON_SITE_PROBE_IPV4_TARGETS"
ENV_IPV6_TARGETS = "NYXMON_SITE_PROBE_IPV6_TARGETS"
ENV_DNS_NAMES = "NYXMON_SITE_PROBE_DNS_NAMES"
ENV_DOWN_AFTER_FAILURES = "NYXMON_SITE_DOWN_AFTER_FAILURES"
ENV_RECOVERY_GRACE = "NYXMON_SITE_RECOVERY_GRACE_SECONDS"
ENV_MAX_HOLD = "NYXMON_SITE_MAX_HOLD_SECONDS"
ENV_INCIDENT_NOTIFY_AFTER = "NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS"
ENV_INCIDENT_REMINDER = "NYXMON_SITE_INCIDENT_REMINDER_SECONDS"

DEFAULT_PROBE_INTERVAL_SECONDS = 60
DEFAULT_PROBE_TIMEOUT_SECONDS = 3
DEFAULT_DOWN_AFTER_FAILURES = 2
DEFAULT_RECOVERY_GRACE_SECONDS = 900
DEFAULT_MAX_HOLD_SECONDS = 3 * 3600
DEFAULT_INCIDENT_NOTIFY_AFTER_SECONDS = 900
DEFAULT_INCIDENT_REMINDER_SECONDS = 6 * 3600

DEFAULT_IPV4_TARGETS = "1.1.1.1:443,8.8.8.8:443,9.9.9.9:443"
DEFAULT_IPV6_TARGETS = (
    "[2606:4700:4700::1111]:443,[2001:4860:4860::8888]:443,[2620:fe::fe]:443"
)
DEFAULT_DNS_NAMES = "cloudflare.com,google.com,quad9.net"

# Warn at most once per (variable, value) so a permanently misconfigured
# environment cannot flood the log on every restart of the loop.
_warned_env_values: set[tuple[str, str]] = set()

# Warn at most once per check about a requirement on an unobserved path.
_warned_unobserved_checks: set[int] = set()


def reset_site_config_warning_state() -> None:
    """Forget which configuration warnings were already emitted (tests only)."""
    _warned_env_values.clear()
    _warned_unobserved_checks.clear()


def _warn_env_once(env_name: str, value: str, reason: str, default: Any) -> None:
    marker = (env_name, value)
    if marker in _warned_env_values:
        return
    _warned_env_values.add(marker)
    logger.warning(
        "%s=%r is invalid (%s); using the default %r",
        env_name,
        value,
        reason,
        default,
    )


def _bounded_env_int(
    source: Mapping[str, str], env_name: str, default: int, low: int, high: int
) -> int:
    raw = source.get(env_name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        _warn_env_once(env_name, raw, "not an integer", default)
        return default
    if parsed < low or parsed > high:
        _warn_env_once(env_name, raw, f"must be between {low} and {high}", default)
        return default
    return parsed


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """One thing to try in a probe round.

    Attributes:
        path: The connectivity path this target belongs to.
        kind: ``tcp`` for an address literal, ``dns`` for a name lookup.
        host: IP literal or DNS name.
        port: TCP port, ``0`` for DNS targets.
    """

    path: str
    kind: str
    host: str
    port: int = 0

    def __str__(self) -> str:
        if self.kind == PROBE_KIND_DNS:
            return self.host
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


def _split_endpoint(token: str) -> tuple[str, str] | None:
    """Split ``host:port`` / ``[v6]:port`` into its parts."""
    if token.startswith("["):
        host, closing, rest = token[1:].partition("]")
        if not closing or not rest.startswith(":"):
            return None
        return host, rest[1:]
    host, separator, port = token.partition(":")
    if not separator or ":" in port:
        return None
    return host, port


def _parse_endpoints(
    source: Mapping[str, str], env_name: str, default: str, *, version: int, path: str
) -> tuple[ProbeTarget, ...]:
    """Parse a comma separated endpoint list; an explicit empty value is valid."""
    raw = source.get(env_name)
    value = default if raw is None else raw.strip()
    if not value:
        return ()
    targets = _endpoints_from_value(value, version=version, path=path)
    if targets is None:
        _warn_env_once(env_name, value, "not a list of ip:port endpoints", default)
        parsed = _endpoints_from_value(default, version=version, path=path)
        return () if parsed is None else parsed
    return targets


def _endpoints_from_value(
    value: str, *, version: int, path: str
) -> tuple[ProbeTarget, ...] | None:
    targets: list[ProbeTarget] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        parts = _split_endpoint(token)
        if parts is None:
            return None
        host, port_text = parts
        try:
            address = ipaddress.ip_address(host)
            port = int(port_text)
        except ValueError:
            return None
        if address.version != version or not 1 <= port <= 65535:
            return None
        targets.append(
            ProbeTarget(path=path, kind=PROBE_KIND_TCP, host=host, port=port)
        )
    if not targets:
        return None
    return tuple(targets)


def _parse_dns_names(source: Mapping[str, str]) -> tuple[ProbeTarget, ...]:
    raw = source.get(ENV_DNS_NAMES)
    value = DEFAULT_DNS_NAMES if raw is None else raw.strip()
    if not value:
        return ()
    names = _dns_names_from_value(value)
    if names is None:
        _warn_env_once(
            ENV_DNS_NAMES, value, "not a list of host names", DEFAULT_DNS_NAMES
        )
        names = _dns_names_from_value(DEFAULT_DNS_NAMES) or ()
    return names


def _dns_names_from_value(value: str) -> tuple[ProbeTarget, ...] | None:
    targets: list[ProbeTarget] = []
    for token in value.split(","):
        name = token.strip()
        if not name:
            continue
        if any(character.isspace() for character in name) or "/" in name:
            return None
        targets.append(ProbeTarget(path="dns", kind=PROBE_KIND_DNS, host=name))
    if not targets:
        return None
    return tuple(targets)


#: Parsed forms of the documented defaults, so a default-constructed config
#: observes exactly what the plan specifies.
DEFAULT_IPV4_PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    _endpoints_from_value(DEFAULT_IPV4_TARGETS, version=4, path="ipv4") or ()
)
DEFAULT_IPV6_PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    _endpoints_from_value(DEFAULT_IPV6_TARGETS, version=6, path="ipv6") or ()
)
DEFAULT_DNS_PROBE_TARGETS: tuple[ProbeTarget, ...] = (
    _dns_names_from_value(DEFAULT_DNS_NAMES) or ()
)


@dataclass(frozen=True, slots=True)
class SiteConnectivityConfig:
    """Resolved configuration of the site connectivity observer."""

    mode: SiteMode = SiteMode.OFF
    probe_interval: int = DEFAULT_PROBE_INTERVAL_SECONDS
    probe_timeout: int = DEFAULT_PROBE_TIMEOUT_SECONDS
    ipv4_targets: tuple[ProbeTarget, ...] = DEFAULT_IPV4_PROBE_TARGETS
    ipv6_targets: tuple[ProbeTarget, ...] = DEFAULT_IPV6_PROBE_TARGETS
    dns_names: tuple[ProbeTarget, ...] = DEFAULT_DNS_PROBE_TARGETS
    down_after_failures: int = DEFAULT_DOWN_AFTER_FAILURES
    recovery_grace_seconds: int = DEFAULT_RECOVERY_GRACE_SECONDS
    max_hold_seconds: int = DEFAULT_MAX_HOLD_SECONDS
    incident_notify_after_seconds: int = DEFAULT_INCIDENT_NOTIFY_AFTER_SECONDS
    incident_reminder_seconds: int = DEFAULT_INCIDENT_REMINDER_SECONDS

    @property
    def stale_after_seconds(self) -> int:
        """A snapshot older than three probe intervals is not trusted."""
        return 3 * self.probe_interval

    def targets_for(self, path: str) -> tuple[ProbeTarget, ...]:
        """Return the configured targets of ``path`` (empty means unobserved)."""
        if path == "ipv4":
            return self.ipv4_targets
        if path == "ipv6":
            return self.ipv6_targets
        if path == "dns":
            return self.dns_names
        return ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SiteConnectivityConfig":
        """Build the configuration from the environment.

        Every value is validated like the existing reliability knobs: an
        invalid value warns once and falls back to the documented default. An
        explicitly empty target list is valid and marks its path
        ``unobserved``.

        Args:
            env: Mapping to read instead of ``os.environ`` (tests).

        Returns:
            The resolved, immutable configuration.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        raw_mode = source.get(ENV_MODE, "").strip().lower()
        if not raw_mode:
            mode = SiteMode.OFF
        else:
            try:
                mode = SiteMode(raw_mode)
            except ValueError:
                _warn_env_once(ENV_MODE, raw_mode, "unknown mode", SiteMode.OFF.value)
                mode = SiteMode.OFF
        return cls(
            mode=mode,
            probe_interval=_bounded_env_int(
                source, ENV_PROBE_INTERVAL, DEFAULT_PROBE_INTERVAL_SECONDS, 15, 600
            ),
            probe_timeout=_bounded_env_int(
                source, ENV_PROBE_TIMEOUT, DEFAULT_PROBE_TIMEOUT_SECONDS, 1, 10
            ),
            ipv4_targets=_parse_endpoints(
                source,
                ENV_IPV4_TARGETS,
                DEFAULT_IPV4_TARGETS,
                version=4,
                path="ipv4",
            ),
            ipv6_targets=_parse_endpoints(
                source,
                ENV_IPV6_TARGETS,
                DEFAULT_IPV6_TARGETS,
                version=6,
                path="ipv6",
            ),
            dns_names=_parse_dns_names(source),
            down_after_failures=_bounded_env_int(
                source, ENV_DOWN_AFTER_FAILURES, DEFAULT_DOWN_AFTER_FAILURES, 1, 10
            ),
            recovery_grace_seconds=_bounded_env_int(
                source, ENV_RECOVERY_GRACE, DEFAULT_RECOVERY_GRACE_SECONDS, 60, 3600
            ),
            max_hold_seconds=_bounded_env_int(
                source, ENV_MAX_HOLD, DEFAULT_MAX_HOLD_SECONDS, 600, 86400
            ),
            incident_notify_after_seconds=_bounded_env_int(
                source,
                ENV_INCIDENT_NOTIFY_AFTER,
                DEFAULT_INCIDENT_NOTIFY_AFTER_SECONDS,
                60,
                86400,
            ),
            incident_reminder_seconds=_bounded_env_int(
                source,
                ENV_INCIDENT_REMINDER,
                DEFAULT_INCIDENT_REMINDER_SECONDS,
                60,
                2592000,
            ),
        )


# ---------------------------------------------------------------- snapshot


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    """Immutable view of one path at the end of a probe round."""

    state: str
    down_since: int = 0
    recovered_at: int = 0
    release_at: int = 0
    last_release_at: int = 0


@dataclass(frozen=True, slots=True)
class SiteConnectivitySnapshot:
    """Immutable view of site connectivity, read by the result handler.

    Attributes:
        paths: One entry per known path name, including ``unobserved`` ones.
        observed_at: Epoch of the last completed probe round.
        mode: The mode the observer runs in.
        incident_id: Identity of the active outage, ``0`` when none is open.
    """

    paths: Mapping[str, PathSnapshot]
    observed_at: int
    mode: SiteMode
    incident_id: int = 0

    def is_fresh(self, now: int, stale_after: int) -> bool:
        """Whether the snapshot is recent enough to base decisions on."""
        return now - self.observed_at <= stale_after

    def _observed(self, requirement: Sequence[str]) -> list[PathSnapshot]:
        observed: list[PathSnapshot] = []
        for name in requirement:
            path = self.paths.get(name)
            if path is None or path.state == PathState.UNOBSERVED:
                continue
            observed.append(path)
        return observed

    def _requirement_met(self, requirement: Sequence[str]) -> bool:
        """One requirement is met unless every observed member blocks.

        An any-of group ignores ``unobserved`` members and is met as soon as
        one observed member is ``up`` or ``failing``. A group whose members
        are all unobserved is met, which is what makes ``["ipv4", "ipv6"]``
        behave exactly like ``ipv4`` on an IPv4-only site.
        """
        observed = self._observed(requirement)
        if not observed:
            return True
        return any(path.state in USABLE_STATES for path in observed)

    def _requirement_usable_since(self, requirement: Sequence[str]) -> int:
        """Since when this requirement has been usable without interruption.

        A requirement is met as soon as **one** observed member is usable, so
        the watermark is the *earliest* release among the members that are
        usable **now**. Members that are ``down`` or ``recovering`` do not
        contribute: they are not what makes the requirement usable, and
        counting their stale ``last_release_at`` would declare a requirement
        continuously usable although every currently usable member was
        released only moments ago.

        A currently usable member that never released contributes ``0``, which
        correctly says that the group was never unmet: during an IPv6-only
        outage the ``["ipv4", "ipv6"]`` group of ``internet`` kept working over
        IPv4, so no sample of an ``internet`` check is stale because of that
        outage. A requirement without observed members is always met and
        therefore never stale; one without a usable member is unmet, so its
        freshness is irrelevant and ``0`` is returned.
        """
        usable = [
            path for path in self._observed(requirement) if path.state in USABLE_STATES
        ]
        if not usable:
            return 0
        return min(path.last_release_at for path in usable)

    def dependency_usable(self, dependency: SiteDependency | None) -> bool:
        """Whether every requirement of ``dependency`` is met right now.

        An unclassified check (``None``) has no site dependency; it is never a
        recovery-recheck candidate, so this returns ``False`` for it.
        """
        if dependency is None:
            return False
        return all(
            self._requirement_met(requirement)
            for requirement in dependency.requirements
        )

    def _reason(
        self, dependency: SiteDependency, claim_started_at: int
    ) -> dict[str, Any] | None:
        blocked: list[str] = []
        for requirement in dependency.requirements:
            if self._requirement_met(requirement):
                continue
            for name in requirement:
                path = self.paths.get(name)
                if path is None or path.state not in BLOCKING_STATES:
                    continue
                if name not in blocked:
                    blocked.append(name)
        if blocked:
            states = {self.paths[name].state for name in blocked}
            down = PathState.DOWN in states
            down_since = [
                self.paths[name].down_since
                for name in blocked
                if self.paths[name].down_since
            ]
            return {
                "reason": (
                    REASON_DEPENDENCY_DOWN if down else REASON_DEPENDENCY_RECOVERING
                ),
                "paths": blocked,
                "down_since": min(down_since) if down_since else 0,
                "state": str(PathState.DOWN if down else PathState.RECOVERING),
                "incident_id": self.incident_id,
            }
        if claim_started_at <= 0:
            # No claim time to compare against; freshness cannot be judged and
            # a permanent watermark must never hold such a sample forever.
            return None
        late: list[str] = []
        watermark = 0
        for requirement in dependency.requirements:
            usable_since = self._requirement_usable_since(requirement)
            if usable_since <= claim_started_at:
                continue
            watermark = max(watermark, usable_since)
            for name in requirement:
                path = self.paths.get(name)
                if path is None or path.state not in USABLE_STATES:
                    continue
                if path.last_release_at <= claim_started_at or name in late:
                    continue
                late.append(name)
        if late:
            return {
                "reason": REASON_MEASURED_BEFORE_RELEASE,
                "paths": late,
                "down_since": 0,
                "state": str(PathState.UP),
                "incident_id": self.incident_id,
                "release_at": watermark,
            }
        return None

    def hold_reason(
        self,
        dependency: SiteDependency | None,
        claim_started_at: int,
        now: int,
        *,
        stale_after: int,
    ) -> dict[str, Any] | None:
        """Why this sample's notification must be held, or ``None``.

        Args:
            dependency: The check's declared dependency, ``None`` when it is
                unclassified.
            claim_started_at: Epoch at which the sample's execution was
                claimed, used to recognise a sample measured before a release.
            now: Current epoch.
            stale_after: Snapshot staleness bound, normally
                ``config.stale_after_seconds``.

        Returns:
            ``None`` when nothing is held: mode is not ``enforce``, the
            snapshot is stale, the check is unclassified, or every requirement
            is met by a fresh sample. Otherwise a reason dict carrying
            ``reason``, ``paths``, ``down_since``, ``state`` and
            ``incident_id``.
        """
        if self.mode is not SiteMode.ENFORCE:
            return None
        return self.observed_reason(
            dependency, claim_started_at, now, stale_after=stale_after
        )

    def observed_reason(
        self,
        dependency: SiteDependency | None,
        claim_started_at: int,
        now: int,
        *,
        stale_after: int,
    ) -> dict[str, Any] | None:
        """Like :meth:`hold_reason` but ignoring the mode.

        In ``observe`` mode the handler records the reason as metadata with
        ``held: false`` so the observer's judgement can be verified before it
        is allowed to suppress anything.
        """
        if dependency is None:
            return None
        if not self.is_fresh(now, stale_after):
            return None
        return self._reason(dependency, claim_started_at)

    def dependency_recovered(
        self,
        dependency: SiteDependency | None,
        claim_started_at: int,
        now: int,
        *,
        stale_after: int,
    ) -> bool:
        """Whether the dependency is observed recovered for a fresh sample.

        Only this re-arms a check's hold budget (``held_since = 0``). A bypass
        because the snapshot is stale or because the hold is exhausted leaves
        the budget untouched, so a persisting outage cannot re-arm it.
        """
        if dependency is None:
            return False
        if not self.is_fresh(now, stale_after):
            return False
        return self._reason(dependency, claim_started_at) is None


class SiteStateProvider(Protocol):
    """What the result handler needs from the observer."""

    def snapshot(self) -> SiteConnectivitySnapshot | None:
        """Return the most recent snapshot, or ``None`` when there is none."""
        ...


class NullSiteState:
    """Site state provider used when the feature is off."""

    def snapshot(self) -> SiteConnectivitySnapshot | None:
        """Always ``None``: nothing is observed, nothing is ever held."""
        return None


# ------------------------------------------------------------------ probes


class ProbeRunner(Protocol):
    """Executes a single probe target."""

    async def probe(self, target: ProbeTarget) -> bool:
        """Return whether ``target`` answered."""
        ...


class DefaultProbeRunner:
    """TCP connects for address literals, ``getaddrinfo`` for DNS names."""

    def __init__(self, *, timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS) -> None:
        self._timeout = float(timeout)

    async def probe(self, target: ProbeTarget) -> bool:
        """Probe one target, never raising.

        Args:
            target: The endpoint or name to try.

        Returns:
            ``True`` when the target answered inside the per-target timeout.
        """
        try:
            with anyio.fail_after(self._timeout):
                if target.kind == PROBE_KIND_DNS:
                    infos = await anyio.getaddrinfo(target.host, None)
                    return bool(infos)
                stream = await anyio.connect_tcp(target.host, target.port)
                await stream.aclose()
                return True
        except Exception:
            logger.debug("site connectivity probe failed for %s", target, exc_info=True)
            return False


# ------------------------------------------------------- store protocols


class SiteIncidentStore(Protocol):
    """The incident-store surface the observer needs (plan section 10)."""

    def get_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        """Return the persisted incident row, if any."""
        ...

    def open_collector_incident(
        self, incident_key: str, *, now: int, payload: dict[str, Any]
    ) -> CollectorIncident:
        """Create the row silently or replace its payload. Atomic."""
        ...

    def claim_collector_incident_alert(
        self,
        incident_key: str,
        *,
        now: int,
        reminder_seconds: int,
        payload: dict[str, Any] | None = None,
    ) -> CollectorIncidentAlert:
        """Unused by the site incident; its alert bookkeeping is in the payload."""
        ...

    def set_collector_incident_payload(
        self, incident_key: str, payload: dict[str, Any]
    ) -> CollectorIncident | None:
        """Replace an existing row's payload; ``None`` when there is no row."""
        ...

    def close_collector_incident(self, incident_key: str) -> CollectorIncident | None:
        """Delete the row. The observer never calls this: the row is permanent."""
        ...


class HeldCheckRow(Protocol):
    """A check whose alerting is currently held by a site dependency."""

    @property
    def check_id(self) -> int: ...

    @property
    def data(self) -> dict[str, Any]: ...

    @property
    def status(self) -> str: ...

    @property
    def disabled(self) -> bool: ...

    @property
    def next_check_time(self) -> int: ...

    @property
    def held_since(self) -> int: ...


class SiteCheckScheduler(Protocol):
    """The check-repository surface the recovery recheck needs."""

    async def list_held_checks_async(self) -> Sequence[HeldCheckRow]:
        """Checks whose notification state has ``held_since > 0``."""
        ...

    async def reschedule_checks_async(
        self, check_ids: list[int], *, run_at: int
    ) -> int:
        """Pull idle, enabled checks forward. Returns the number of rows changed."""
        ...

    async def count_held_checks_async(self) -> int:
        """How many checks are currently held.

        The observer uses this variant rather than the synchronous
        ``count_held_checks``: on the real repositories the synchronous form
        goes through the blocking portal, which must never be entered from the
        event loop the observer runs on.
        """
        ...

    def count_held_checks(self) -> int:
        """Synchronous equivalent, kept for callers outside the event loop."""
        ...

    async def count_processing_claims_before_async(self, epoch: int) -> int:
        """Executions claimed before ``epoch`` that are still processing."""
        ...


#: ``(incident_key, name, error_type, error_msg, status, opsgate_ticket)`` ->
#: ``True`` when the message was delivered, ``False`` when the send failed.
SiteNotifier: TypeAlias = Callable[[str, str, str, str, str, bool], bool]


# ------------------------------------------------------------- observer


@dataclass
class _PathRuntime:
    """Mutable per-path state of the observer."""

    name: str
    state: str
    down_since: int = 0
    recovered_at: int = 0
    release_at: int = 0
    last_release_at: int = 0
    failures: int = 0


def _iso(epoch: int) -> str:
    if epoch <= 0:
        return "unknown"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    minutes, remainder = divmod(max(seconds, 0), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    """Coerce a persisted JSON value to a mapping, defaulting to empty."""
    return value if isinstance(value, dict) else {}


def _render_summary_line(
    record: Mapping[str, Any], names: Sequence[str], omitted: int
) -> str:
    raw_paths = _as_mapping(record.get("paths"))
    parts = [
        f"{name} down "
        f"{_iso(_as_int(_as_mapping(raw_paths.get(name)).get('down_since')))} "
        f"until {_iso(_as_int(_as_mapping(raw_paths.get(name)).get('recovered_at')))}"
        for name in names
    ]
    if omitted > 0:
        parts.append(f"+{omitted} more path(s)")
    path_text = "; ".join(parts)
    duration = max(
        _as_int(record.get("ended_at")) - _as_int(record.get("started_at")), 0
    )
    held = _as_int(record.get("held_at_close"))
    return (
        f"Site connectivity recovered: outage "
        f"{_as_int(record.get('incident_id'))} lasted {_duration(duration)}"
        + (f" ({path_text})" if path_text else "")
        + ". Ongoing alert delivered: "
        + ("yes" if record.get("ongoing_delivered") else "no")
        + f". {held} dependent check(s) were held at close and are "
        "being rechecked."
    )


def _summary_line(record: Mapping[str, Any], *, limit: int) -> str:
    """Render one summary record, capping its path list to fit ``limit``.

    Args:
        record: The persisted summary record.
        limit: Character budget for this single line.

    Returns:
        The rendered line. A record that does not fit even without any path
        detail is hard truncated, because dropping it would lose the outage.
    """
    names = sorted(str(name) for name in _as_mapping(record.get("paths")))
    kept = len(names)
    line = _render_summary_line(record, names, 0)
    while len(line) > limit and kept > 0:
        kept -= 1
        line = _render_summary_line(record, names[:kept], len(names) - kept)
    if len(line) > limit:
        line = line[:limit]
    return line


def _record_is_due(record: Mapping[str, Any], now: int) -> bool:
    """Whether this pending record may be (re)sent right now.

    Args:
        record: A pending summary record.
        now: Current epoch.

    Returns:
        ``True`` when the record was never attempted, or when its last attempt
        is at least :data:`SITE_DELIVERY_RETRY_SECONDS` old.
    """
    attempt_at = _as_int(record.get("attempt_at"))
    return attempt_at <= 0 or now - attempt_at >= SITE_DELIVERY_RETRY_SECONDS


def warn_unobserved_requirements(
    checks: Iterable[Any], unobserved_paths: Iterable[str]
) -> None:
    """Warn once per check that requires a path nobody observes.

    Such a requirement is always met and can therefore never hold, which is
    almost certainly a misclassification (for example ``{"requires": ["ipv6"]}``
    on a site with no IPv6 targets configured).

    Args:
        checks: The checks to inspect; anything exposing ``data``.
        unobserved_paths: Names of the paths without configured targets.
    """
    unobserved = frozenset(unobserved_paths)
    if not unobserved:
        return
    for check in checks:
        dependency = resolve_site_dependency(check)
        if dependency is None:
            continue
        offenders = [
            requirement[0]
            for requirement in dependency.requirements
            if len(requirement) == 1 and requirement[0] in unobserved
        ]
        if not offenders:
            continue
        check_id = int(getattr(check, "check_id", 0) or 0)
        if check_id in _warned_unobserved_checks:
            continue
        _warned_unobserved_checks.add(check_id)
        logger.warning(
            "check_id=%s requires the unobserved path(s) %s; the requirement is "
            "always met and can never hold a notification",
            check_id,
            ", ".join(offenders),
        )


class SiteConnectivityObserver:
    """Probes connectivity, coordinates the site incident, drives rechecks.

    One instance runs as a second task on the collector's portal. It never
    blocks check collection and never raises into its caller: probe, store and
    notifier failures are logged and retried on the next round.
    """

    def __init__(
        self,
        config: SiteConnectivityConfig,
        *,
        incident_store: SiteIncidentStore,
        scheduler: SiteCheckScheduler,
        probe_runner: ProbeRunner | None = None,
        clock: Callable[[], float] | None = None,
        notifier: SiteNotifier | None = None,
    ) -> None:
        self._config = config
        self._store = incident_store
        self._scheduler = scheduler
        self._probe_runner = probe_runner or DefaultProbeRunner(
            timeout=config.probe_timeout
        )
        self._clock: Callable[[], float] = clock or time.time
        self._notifier = notifier

        self._paths: dict[str, _PathRuntime] = {}
        for name in PATH_NAMES:
            observed = bool(config.targets_for(name))
            self._paths[name] = _PathRuntime(
                name=name,
                state=str(PathState.UP if observed else PathState.UNOBSERVED),
            )
        self._observed_at = 0
        self._phase = PHASE_IDLE
        self._incident_id = 0
        self._ongoing: dict[str, Any] = {}
        self._summaries: list[dict[str, Any]] = []
        self._recheck: dict[str, Any] = {}

        self._lock = threading.Lock()
        self._snapshot: SiteConnectivitySnapshot | None = None
        self._running = False
        self._row_present = False
        # Until the persisted row was read once, this observer knows nothing
        # about the lifecycle and must never write over it.
        self._restored = False
        self._publish_snapshot()

    # ------------------------------------------------------------- public

    @property
    def config(self) -> SiteConnectivityConfig:
        """The configuration this observer runs with."""
        return self._config

    def snapshot(self) -> SiteConnectivitySnapshot | None:
        """Return the current snapshot; safe to call from any thread."""
        with self._lock:
            return self._snapshot

    def unobserved_paths(self) -> tuple[str, ...]:
        """Names of the paths without configured targets."""
        return tuple(
            name
            for name, path in self._paths.items()
            if path.state == PathState.UNOBSERVED
        )

    def warn_unobserved_requirements(self, checks: Iterable[Any]) -> None:
        """Warn once per check that requires a path this site does not observe."""
        warn_unobserved_requirements(checks, self.unobserved_paths())

    def stop(self) -> None:
        """Ask :meth:`run` to return after the current interval."""
        self._running = False

    async def run(self) -> None:
        """Restore persisted state, then probe every ``probe_interval``."""
        if self._config.mode is SiteMode.OFF:
            return
        self._running = True
        await self.restore()
        while self._running:
            try:
                await self.run_once()
            except Exception:
                logger.exception("site connectivity round failed")
            if not self._running:
                break
            await self._wait_for_next_round()

    async def _wait_for_next_round(self) -> None:
        await anyio.sleep(self._config.probe_interval)

    async def restore(self) -> None:
        """Restore the whole lifecycle from the persisted row (plan 5.3).

        Path states and timestamps, the release watermarks, ``observed_at``,
        the alert bookkeeping and the recheck work item all come back, so an
        attempted alert is not attempted again before its retry is due and a
        grace is neither restarted nor released early. An ``active`` phase
        whose ``observed_at`` is stale is downgraded to ``idle`` (plan 7.6):
        after a long disable the persisted outage is no longer evidence.

        A failed read leaves the observer unrestored: no round runs and
        nothing is written until a later call succeeds, because an empty
        lifecycle written over the persisted row would erase pending
        summaries, the ongoing intent and the release watermarks. An absent
        row counts as restored; there is simply nothing to load.
        """
        if self._config.mode is SiteMode.OFF:
            return
        try:
            incident = await self._store_get()
        except Exception:
            logger.exception("failed to restore the site connectivity incident")
            return
        self._restored = True
        if incident is None:
            self._publish_snapshot()
            return
        self._row_present = True
        payload = incident.payload or {}
        version = payload.get("version", SITE_PAYLOAD_VERSION)
        if version != SITE_PAYLOAD_VERSION:
            logger.warning(
                "ignoring the site connectivity payload: unknown version %r", version
            )
            self._publish_snapshot()
            return

        raw_paths = payload.get("paths")
        if isinstance(raw_paths, dict):
            for name, raw in raw_paths.items():
                path = self._paths.get(name)
                if path is None or path.state == PathState.UNOBSERVED:
                    continue
                if not isinstance(raw, dict):
                    continue
                state = raw.get("state")
                if state not in (
                    PathState.UP,
                    PathState.FAILING,
                    PathState.DOWN,
                    PathState.RECOVERING,
                ):
                    continue
                path.state = str(state)
                path.down_since = _as_int(raw.get("down_since"))
                path.recovered_at = _as_int(raw.get("recovered_at"))
                path.release_at = _as_int(raw.get("release_at"))
                path.last_release_at = _as_int(raw.get("last_release_at"))
                # The failure counter is derived, not persisted: a restored
                # ``failing`` path carries the one failure its state implies.
                path.failures = 1 if state == PathState.FAILING else 0

        self._observed_at = _as_int(payload.get("observed_at"))
        self._phase = (
            PHASE_ACTIVE if payload.get("phase") == PHASE_ACTIVE else PHASE_IDLE
        )
        self._incident_id = _as_int(payload.get("incident_id"))
        ongoing = payload.get("ongoing")
        self._ongoing = dict(ongoing) if isinstance(ongoing, dict) else {}
        summaries = payload.get("summaries")
        self._summaries = (
            [dict(record) for record in summaries if isinstance(record, dict)]
            if isinstance(summaries, list)
            else []
        )
        recheck = payload.get("recheck")
        self._recheck = dict(recheck) if isinstance(recheck, dict) else {}

        now = int(self._clock())
        if self._phase == PHASE_ACTIVE and not self._is_fresh(now):
            logger.warning(
                "site connectivity state observed at %s is stale; the active "
                "outage %s is set to idle, keeping release watermarks, pending "
                "summaries and the recheck item",
                _iso(self._observed_at),
                self._incident_id,
            )
            self._retire_ongoing()
            for path in self._paths.values():
                if path.state == PathState.UNOBSERVED:
                    continue
                path.state = str(PathState.UP)
                path.down_since = 0
                path.recovered_at = 0
                path.release_at = 0
                path.failures = 0
            self._phase = PHASE_IDLE
            self._incident_id = 0
        self._publish_snapshot()

    async def run_once(self) -> None:
        """Run one round: probe, state machine, persistence, alerts, recheck.

        A round that starts before the persisted row was read successfully
        retries the restore and, if that fails again, does nothing at all: it
        neither probes nor writes, and the snapshot stays as stale as it was,
        so nothing new is held while the lifecycle is unknown.
        """
        if self._config.mode is SiteMode.OFF:
            return
        if not self._restored:
            await self.restore()
            if not self._restored:
                logger.warning(
                    "skipping the site connectivity round: the persisted "
                    "lifecycle could not be restored yet"
                )
                return
        now = int(self._clock())
        outcomes = await self._probe_round()
        released = self._advance_paths(outcomes, now)
        self._observed_at = now
        await self._advance_incident(now, released)
        self._publish_snapshot()
        await self._write_payload(now)
        await self._process_ongoing(now)
        await self._process_summaries(now)
        await self._process_recheck(now)

    # -------------------------------------------------------------- probes

    def _targets(self) -> tuple[ProbeTarget, ...]:
        targets: list[ProbeTarget] = []
        for name in PATH_NAMES:
            targets.extend(self._config.targets_for(name))
        return tuple(targets)

    async def _probe_round(self) -> dict[str, bool]:
        """Probe every target concurrently; return ``path -> round failed``."""
        targets = self._targets()
        if not targets:
            return {}
        outcomes: dict[ProbeTarget, bool] = {}

        async def run_target(target: ProbeTarget) -> None:
            try:
                outcomes[target] = bool(await self._probe_runner.probe(target))
            except Exception:
                logger.exception("site connectivity probe raised for %s", target)
                outcomes[target] = False

        budget = self._config.probe_timeout + PROBE_ROUND_SLACK_SECONDS
        with anyio.move_on_after(budget):
            async with anyio.create_task_group() as task_group:
                for target in targets:
                    task_group.start_soon(run_target, target)

        failed: dict[str, bool] = {}
        for name in PATH_NAMES:
            path_targets = self._config.targets_for(name)
            if not path_targets:
                continue
            # A round fails only when every target failed: one dead server is
            # not evidence of an outage. Targets that did not finish inside the
            # round budget count as failed.
            failed[name] = not any(
                outcomes.get(target, False) for target in path_targets
            )
        return failed

    # ------------------------------------------------------- state machine

    def _advance_paths(
        self, outcomes: Mapping[str, bool], now: int
    ) -> list[tuple[str, int]]:
        """Apply one round to every path; return the releases it produced.

        The round's own outcome is applied **first**: a failed round exactly at
        the release boundary returns a ``recovering`` path to ``down`` instead
        of releasing it, so a still-broken path can never close the incident
        and drop the holds.
        """
        released: list[tuple[str, int]] = []
        for name, path in self._paths.items():
            if path.state == PathState.UNOBSERVED:
                continue
            if name in outcomes:
                self._apply_outcome(path, outcomes[name], now)
            if path.state == PathState.RECOVERING and now >= path.release_at:
                path.last_release_at = path.release_at
                path.state = str(PathState.UP)
                path.release_at = 0
                path.failures = 0
                released.append((name, path.last_release_at))
        return released

    def _apply_outcome(self, path: _PathRuntime, failed: bool, now: int) -> None:
        if failed:
            if path.state in (PathState.UP, PathState.FAILING):
                path.failures += 1
                path.state = str(PathState.FAILING)
                if path.failures >= self._config.down_after_failures:
                    path.state = str(PathState.DOWN)
                    path.down_since = now
                    path.recovered_at = 0
                    path.release_at = 0
                    path.failures = 0
            elif path.state == PathState.RECOVERING:
                # No re-confirmation: the incident is established, so a single
                # failed round returns the path to ``down`` and keeps
                # ``down_since``. Closing needs one uninterrupted grace.
                path.state = str(PathState.DOWN)
                path.recovered_at = 0
                path.release_at = 0
            return
        path.failures = 0
        if path.state == PathState.FAILING:
            path.state = str(PathState.UP)
        elif path.state == PathState.DOWN:
            path.state = str(PathState.RECOVERING)
            path.recovered_at = now
            path.release_at = now + self._config.recovery_grace_seconds

    def _observed_paths(self) -> list[_PathRuntime]:
        return [
            path for path in self._paths.values() if path.state != PathState.UNOBSERVED
        ]

    # ------------------------------------------------- incident lifecycle

    async def _advance_incident(
        self, now: int, released: list[tuple[str, int]]
    ) -> None:
        """Move the phase machine; every change is persisted by one write."""
        observed = self._observed_paths()
        down = [path for path in observed if path.state == PathState.DOWN]
        active = [path for path in observed if path.state in BLOCKING_STATES]

        if self._phase == PHASE_IDLE:
            if down:
                self._phase = PHASE_ACTIVE
                self._incident_id = min(path.down_since for path in down)
                logger.info(
                    "site connectivity incident %s opened: %s down",
                    self._incident_id,
                    ", ".join(sorted(path.name for path in down)),
                )
            return

        if not down:
            # The last ``down`` path became ``recovering``: an obsolete,
            # undelivered ongoing alert must never be sent afterwards.
            self._retire_ongoing()

        if active:
            if released:
                self._open_recheck(max(release for _, release in released))
            return

        await self._close_incident(now, released)

    def _retire_ongoing(self) -> None:
        ongoing = self._ongoing
        if not ongoing or ongoing.get("incident_id") != self._incident_id:
            return
        if ongoing.get("delivered") or ongoing.get("retired"):
            return
        self._ongoing = {**ongoing, "retired": True}

    def _open_recheck(self, release_at: int) -> None:
        if self._recheck.get("pending"):
            release_at = max(release_at, _as_int(self._recheck.get("release_at")))
        self._recheck = {"pending": True, "release_at": release_at}

    async def _close_incident(self, now: int, released: list[tuple[str, int]]) -> None:
        incident_id = self._incident_id
        participants = [
            path
            for path in self._observed_paths()
            if path.down_since >= incident_id > 0 and path.recovered_at > 0
        ]
        last_recovered_at = max(
            (path.recovered_at for path in participants), default=now
        )
        duration = max(last_recovered_at - incident_id, 0)
        delivered = bool(
            self._ongoing.get("delivered")
            and self._ongoing.get("incident_id") == incident_id
        )
        eligible = duration >= self._config.incident_notify_after_seconds or delivered
        already = any(
            record.get("incident_id") == incident_id for record in self._summaries
        )
        if eligible and not already:
            self._summaries.append(
                {
                    "incident_id": incident_id,
                    "started_at": incident_id,
                    "ended_at": last_recovered_at,
                    "paths": {
                        path.name: {
                            "down_since": path.down_since,
                            "recovered_at": path.recovered_at,
                        }
                        for path in participants
                    },
                    "ongoing_delivered": delivered,
                    "held_at_close": await self._count_held_checks(),
                    "attempt_at": 0,
                    "attempts": 0,
                }
            )
        release_at = max(
            (release for _, release in released),
            default=max((path.last_release_at for path in participants), default=now),
        )
        self._open_recheck(release_at)
        logger.info(
            "site connectivity incident %s closed after %s (summary: %s)",
            incident_id,
            _duration(duration),
            "yes" if eligible else "no",
        )
        self._phase = PHASE_IDLE
        self._incident_id = 0

    # ------------------------------------------------------------- alerts

    async def _process_ongoing(self, now: int) -> None:
        """Send the ongoing outage alert, writing the intent before the send."""
        if self._phase != PHASE_ACTIVE:
            return
        down = [path for path in self._observed_paths() if path.state == PathState.DOWN]
        if not down:
            return
        if now - self._incident_id < self._config.incident_notify_after_seconds:
            return
        ongoing = self._ongoing
        same_incident = ongoing.get("incident_id") == self._incident_id
        last_alert_at = _as_int(ongoing.get("last_alert_at")) if same_incident else 0
        attempts = _as_int(ongoing.get("attempts")) if same_incident else 0
        if same_incident:
            if ongoing.get("delivered"):
                if now - last_alert_at < self._config.incident_reminder_seconds:
                    return
            elif ongoing.get("retired"):
                # The retired intent itself is never retried, but a path that
                # flaps back to ``down`` may earn a *new* attempt once the
                # reminder cadence has elapsed since the last attempt or the
                # last delivery, whichever is later. Without that clock an
                # undelivered, retired alert would silence the incident for
                # good.
                reference = max(last_alert_at, _as_int(ongoing.get("attempt_at")))
                if now - reference < self._config.incident_reminder_seconds:
                    return
            elif now - _as_int(ongoing.get("attempt_at")) < SITE_DELIVERY_RETRY_SECONDS:
                return

        self._ongoing = {
            "incident_id": self._incident_id,
            "attempt_at": now,
            "attempts": attempts + 1,
            "delivered": False,
            "retired": False,
            "last_alert_at": last_alert_at,
        }
        if not await self._write_payload(now):
            # The intent must be durable before the send; without it a crash
            # would lose the retry obligation.
            return
        delivered = await self._send(
            error_type=SITE_ONGOING_ERROR_TYPE,
            error_msg=self._ongoing_message(down, now),
            status="error",
            opsgate_ticket=True,
        )
        if not delivered:
            return
        self._ongoing = {**self._ongoing, "delivered": True, "last_alert_at": now}
        await self._write_payload(now)

    def _ongoing_message(self, down: list[_PathRuntime], now: int) -> str:
        parts = [
            f"{path.name} down since {_iso(path.down_since)} "
            f"({_duration(now - path.down_since)})"
            for path in sorted(down, key=lambda path: path.name)
        ]
        return (
            f"Site connectivity outage {self._incident_id}: "
            + ", ".join(parts)
            + ". Alerts of dependent checks are held for at most "
            + _duration(self._config.max_hold_seconds)
            + "."
        )

    async def _process_summaries(self, now: int) -> None:
        """Send one size-bounded batch of pending summary records.

        Telegram rejects a message above 4096 characters, so a backlog is
        drained oldest first in batches of at most
        :data:`SITE_SUMMARY_MAX_RECORDS` records and
        :data:`SITE_SUMMARY_MAX_CHARS` characters. Only the records the batch
        covers get an ``attempt_at`` and only they are removed on success; the
        next round sends the next batch. A record that was attempted less than
        :data:`SITE_DELIVERY_RETRY_SECONDS` ago is not eligible, so a record
        appended right after a failed send cannot pull that send forward.
        """
        if not self._summaries:
            return
        batch, message = self._summary_batch(now)
        if not batch:
            return
        covered = {_as_int(record.get("incident_id")) for record in batch}
        self._summaries = [
            (
                {
                    **record,
                    "attempt_at": now,
                    "attempts": _as_int(record.get("attempts")) + 1,
                }
                if _as_int(record.get("incident_id")) in covered
                else record
            )
            for record in self._summaries
        ]
        if not await self._write_payload(now):
            return
        delivered = await self._send(
            error_type=SITE_SUMMARY_ERROR_TYPE,
            error_msg=message,
            status="warning",
            opsgate_ticket=False,
        )
        if not delivered:
            return
        # Only the records this send covered are removed; a record appended by
        # a later round and every record of a later batch is kept.
        self._summaries = [
            record
            for record in self._summaries
            if _as_int(record.get("incident_id")) not in covered
        ]
        await self._write_payload(now)

    def _summary_batch(self, now: int) -> tuple[list[dict[str, Any]], str]:
        """Pick the oldest pending records that are due and fit one message.

        Records are taken strictly oldest first and every record of the batch
        must be individually due: never attempted, or attempted at least
        :data:`SITE_DELIVERY_RETRY_SECONDS` ago. The scan stops at the first
        record that is not due, so a record appended one probe tick after a
        failed send neither jumps the queue nor shortens the retry bound of the
        older records ahead of it.

        Args:
            now: Current epoch, used to judge the retry bound.

        Returns:
            The covered records and the rendered message body, empty when the
            oldest pending record is not due yet. A single record that alone
            exceeds the character budget is truncated rather than dropped, so
            it cannot block the backlog forever.
        """
        ordered = sorted(
            self._summaries,
            key=lambda record: (
                _as_int(record.get("started_at")),
                _as_int(record.get("incident_id")),
            ),
        )
        batch: list[dict[str, Any]] = []
        lines: list[str] = []
        used = 0
        for record in ordered:
            if not _record_is_due(record, now):
                break
            if len(batch) >= SITE_SUMMARY_MAX_RECORDS:
                break
            line = _summary_line(record, limit=SITE_SUMMARY_MAX_CHARS)
            separator = 1 if batch else 0
            if batch and used + separator + len(line) > SITE_SUMMARY_MAX_CHARS:
                break
            batch.append(record)
            lines.append(line)
            used += separator + len(line)
        return batch, "\n".join(lines)

    async def _send(
        self, *, error_type: str, error_msg: str, status: str, opsgate_ticket: bool
    ) -> bool:
        notifier = self._notifier
        if notifier is None:
            logger.error("the site connectivity notifier is not configured")
            return False
        try:
            outcome = await to_thread.run_sync(
                notifier,
                SITE_INCIDENT_KEY,
                SITE_INCIDENT_NAME,
                error_type,
                error_msg,
                status,
                opsgate_ticket,
                abandon_on_cancel=False,
            )
        except Exception:
            logger.exception("failed to send the site connectivity %s", error_type)
            return False
        # ``None`` means "the notifier cannot tell"; treated as delivered, like
        # the per-check path of plan section 8.
        return outcome is not False

    # ------------------------------------------------------------ recheck

    async def _process_recheck(self, now: int) -> None:
        """Reschedule held dependents whose complete dependency is usable.

        The in-flight count is read **before** the held set (plan 7.4). A
        pre-release claim that commits between the two reads was counted, so
        the work item stays pending; one that committed before the count is
        visible to the held query. The two reads therefore never both miss the
        same check.
        """
        if self._config.mode is not SiteMode.ENFORCE:
            return
        if not self._recheck.get("pending"):
            return
        release_at = _as_int(self._recheck.get("release_at"))
        try:
            in_flight = await self._count_processing_claims_before(release_at)
            held = await self._list_held_checks()
        except Exception:
            logger.exception("site connectivity recheck query failed")
            return

        snapshot = self.snapshot()
        usable: list[HeldCheckRow] = []
        for row in held:
            dependency = resolve_site_dependency(row)
            if dependency is None:
                continue
            if snapshot is not None and snapshot.dependency_usable(dependency):
                usable.append(row)
        due = [
            row.check_id
            for row in usable
            if row.status == "idle" and not row.disabled and row.next_check_time > now
        ]
        if due:
            try:
                await self._reschedule(due, now)
            except Exception:
                logger.exception("failed to reschedule held checks %s", due)
                return
        pending = in_flight > 0 or bool(usable)
        if pending == bool(self._recheck.get("pending")):
            return
        self._recheck = {"pending": pending, "release_at": release_at}
        logger.info(
            "site connectivity recheck for release %s completed", _iso(release_at)
        )
        await self._write_payload(now)

    # ------------------------------------------------------- persistence

    def _is_fresh(self, now: int) -> bool:
        return now - self._observed_at <= self._config.stale_after_seconds

    def _publish_snapshot(self) -> None:
        snapshot = SiteConnectivitySnapshot(
            paths={
                name: PathSnapshot(
                    state=path.state,
                    down_since=path.down_since,
                    recovered_at=path.recovered_at,
                    release_at=path.release_at,
                    last_release_at=path.last_release_at,
                )
                for name, path in self._paths.items()
            },
            observed_at=self._observed_at,
            mode=self._config.mode,
            incident_id=self._incident_id,
        )
        with self._lock:
            self._snapshot = snapshot

    def build_payload(self) -> dict[str, Any]:
        """The whole persisted lifecycle as one payload (plan 5.3)."""
        return {
            "incident_type": SITE_INCIDENT_TYPE,
            "version": SITE_PAYLOAD_VERSION,
            "phase": self._phase,
            "incident_id": self._incident_id,
            "observed_at": self._observed_at,
            "paths": {
                name: {
                    "state": path.state,
                    "down_since": path.down_since,
                    "recovered_at": path.recovered_at,
                    "release_at": path.release_at,
                    "last_release_at": path.last_release_at,
                }
                for name, path in self._paths.items()
                if path.state != PathState.UNOBSERVED
            },
            "ongoing": dict(self._ongoing),
            "summaries": [dict(record) for record in self._summaries],
            "recheck": dict(self._recheck),
        }

    async def _write_payload(self, now: int) -> bool:
        """Persist the current lifecycle in one atomic write."""
        payload = self.build_payload()
        try:
            if self._row_present:
                updated = await self._store_set(payload)
                if updated is not None:
                    return True
                self._row_present = False
            await self._store_open(payload, now)
            self._row_present = True
            return True
        except Exception:
            logger.exception("failed to persist the site connectivity incident")
            self._row_present = False
            return False

    async def _store_get(self) -> CollectorIncident | None:
        impl = getattr(self._store, "_get_collector_incident_async", None)
        if impl is not None:
            result: CollectorIncident | None = await impl(SITE_INCIDENT_KEY)
            return result
        return await to_thread.run_sync(
            self._store.get_collector_incident,
            SITE_INCIDENT_KEY,
            abandon_on_cancel=False,
        )

    async def _store_open(self, payload: dict[str, Any], now: int) -> CollectorIncident:
        impl = getattr(self._store, "_open_collector_incident_async", None)
        if impl is not None:
            opened: CollectorIncident = await impl(SITE_INCIDENT_KEY, now, payload)
            return opened
        return await to_thread.run_sync(
            partial(
                self._store.open_collector_incident,
                SITE_INCIDENT_KEY,
                now=now,
                payload=payload,
            ),
            abandon_on_cancel=False,
        )

    async def _store_set(self, payload: dict[str, Any]) -> CollectorIncident | None:
        impl = getattr(self._store, "_set_collector_incident_payload_async", None)
        if impl is not None:
            updated: CollectorIncident | None = await impl(SITE_INCIDENT_KEY, payload)
            return updated
        return await to_thread.run_sync(
            self._store.set_collector_incident_payload,
            SITE_INCIDENT_KEY,
            payload,
            abandon_on_cancel=False,
        )

    # -------------------------------------------------------- scheduler

    async def _count_held_checks(self) -> int:
        """Count held checks for a summary record, never raising into a round.

        The asynchronous form is used whenever the scheduler offers one: the
        synchronous ``count_held_checks`` of the real repositories enters the
        blocking portal, which the observer's event loop must not do.
        """
        try:
            impl = getattr(self._scheduler, "count_held_checks_async", None)
            if impl is not None:
                counted: int = await impl()
                return counted
            return await to_thread.run_sync(
                self._scheduler.count_held_checks, abandon_on_cancel=False
            )
        except Exception:
            logger.exception("failed to count held checks")
            return 0

    async def _count_processing_claims_before(self, epoch: int) -> int:
        return await self._scheduler.count_processing_claims_before_async(epoch)

    async def _list_held_checks(self) -> Sequence[HeldCheckRow]:
        return await self._scheduler.list_held_checks_async()

    async def _reschedule(self, check_ids: list[int], run_at: int) -> int:
        return await self._scheduler.reschedule_checks_async(check_ids, run_at=run_at)


def _as_int(value: Any) -> int:
    """Coerce a persisted JSON value to ``int``, defaulting to ``0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
