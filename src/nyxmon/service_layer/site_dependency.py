"""Parsing of per-check site connectivity dependencies.

A check declares which connectivity paths it needs through
``HealthCheck.data["site_dependency"]``. The declaration is validated in the
same fail-safe style as ``notification_policy`` and
``notification_suppression``: anything malformed is warned about once per check
and falls back to "unclassified", never raising into the hot path and never
accidentally suppressing an alert.

Accepted forms (plan section 7.1)::

    {"site_dependency": "internet"}
    {"site_dependency": {"requires": ["dns", ["ipv4", "ipv6"]]}}
    {"site_dependency": {"requires": ["ipv6"]}}
    {"site_dependency": "none"}

``requires`` is a list of requirements. Each requirement is either a path name
or a list of alternative path names (an any-of group). A check is affected when
*any* requirement is unmet. ``"internet"`` is shorthand for
``{"requires": ["dns", ["ipv4", "ipv6"]]}``: HTTP and TCP checks connect through
Happy Eyeballs, so a dual-stack host stays reachable while only one address
family is broken.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The connectivity dimensions the observer can measure.
PATH_NAMES: tuple[str, ...] = ("dns", "ipv4", "ipv6")

#: Key under ``HealthCheck.data`` that carries the declaration.
SITE_DEPENDENCY_DATA_KEY = "site_dependency"

#: ``"internet"`` shorthand: a name lookup plus at least one address family.
INTERNET_REQUIREMENTS: tuple[tuple[str, ...], ...] = (("dns",), ("ipv4", "ipv6"))

#: Values that explicitly mean "no site dependency".
UNCLASSIFIED_VALUES = frozenset({"none", ""})

# Warn at most once per check so a single malformed check cannot flood the log
# on every scrape.
_warned_checks: set[int] = set()


def reset_dependency_warning_state() -> None:
    """Forget which checks have already been warned about (tests only)."""
    _warned_checks.clear()


@dataclass(frozen=True, slots=True)
class SiteDependency:
    """The connectivity requirements of one check.

    Attributes:
        requirements: One entry per requirement. Each entry is a tuple of
            alternative path names; a single-element tuple is a plain
            requirement, a longer tuple is an any-of group.
    """

    requirements: tuple[tuple[str, ...], ...]

    @property
    def path_names(self) -> tuple[str, ...]:
        """Every path named by any requirement, in declaration order."""
        seen: list[str] = []
        for requirement in self.requirements:
            for name in requirement:
                if name not in seen:
                    seen.append(name)
        return tuple(seen)


def _warn_once(check_id: int, reason: str, raw: Any) -> None:
    if check_id in _warned_checks:
        return
    _warned_checks.add(check_id)
    logger.warning(
        "%s for check_id=%s is invalid (%s, got %r); treating the check as "
        "unclassified",
        SITE_DEPENDENCY_DATA_KEY,
        check_id,
        reason,
        raw,
    )


def _path_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    if name not in PATH_NAMES:
        return None
    return name


def _requirement(value: Any) -> tuple[str, ...] | None:
    """Normalise one requirement, or return ``None`` when it is malformed."""
    if isinstance(value, str):
        name = _path_name(value)
        return None if name is None else (name,)
    if isinstance(value, (list, tuple)):
        names: list[str] = []
        for member in value:
            name = _path_name(member)
            if name is None:
                return None
            if name not in names:
                names.append(name)
        if not names:
            return None
        return tuple(names)
    return None


def resolve_site_dependency(check: Any) -> SiteDependency | None:
    """Resolve the declared site dependency of ``check``.

    Args:
        check: Anything exposing ``data`` (and optionally ``check_id``); a
            plain ``dict`` of check data is accepted as well.

    Returns:
        The parsed dependency, or ``None`` when the check is unclassified.
        Malformed declarations warn once per check and resolve to ``None``,
        which keeps today's notification behaviour byte for byte.
    """
    data = check if isinstance(check, dict) else getattr(check, "data", None)
    if not isinstance(data, dict):
        return None
    raw = data.get(SITE_DEPENDENCY_DATA_KEY)
    if raw is None:
        return None

    check_id = int(getattr(check, "check_id", 0) or 0)

    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in UNCLASSIFIED_VALUES:
            return None
        if value == "internet":
            return SiteDependency(INTERNET_REQUIREMENTS)
        _warn_once(check_id, "unknown shorthand", raw)
        return None

    if not isinstance(raw, dict):
        _warn_once(check_id, "must be a string or an object", raw)
        return None

    requires = raw.get("requires")
    if not isinstance(requires, (list, tuple)) or not requires:
        _warn_once(check_id, "requires must be a non-empty list", raw)
        return None

    requirements: list[tuple[str, ...]] = []
    for entry in requires:
        requirement = _requirement(entry)
        if requirement is None:
            _warn_once(
                check_id,
                f"every requirement must name one of {', '.join(PATH_NAMES)}",
                raw,
            )
            return None
        if requirement not in requirements:
            requirements.append(requirement)
    return SiteDependency(tuple(requirements))
