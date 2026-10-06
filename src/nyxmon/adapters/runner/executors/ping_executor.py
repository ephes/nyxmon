"""ICMP ping check executor backed by the system ``ping`` binary.

The executor never opens raw ICMP sockets itself. Each attempt runs the
platform's ``ping`` binary for a single echo request, which already carries the
privilege it needs (setuid root on macOS/BSD, ``CAP_NET_RAW`` or
``net.ipv4.ping_group_range`` on Linux). The agent therefore keeps running
unprivileged.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence

import anyio

from ....domain import Check, Result, ResultStatus
from ....domain.ping_config import PingCheckConfig, normalize_ping_target


#: Extra seconds allowed beyond the per-attempt timeout for process start-up
#: and for the binary's own reply wait to expire before the process is killed.
PROCESS_GRACE_SECONDS = 2.0

#: Locations probed when ``ping`` is not on ``PATH`` (minimal service PATHs).
FALLBACK_PING_PATHS = ("/sbin/ping", "/bin/ping", "/usr/bin/ping", "/usr/sbin/ping")
FALLBACK_PING6_PATHS = ("/sbin/ping6", "/usr/sbin/ping6", "/bin/ping6")

MAX_ERROR_DETAIL = 200

_RTT_RE = re.compile(r"time\s*[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*ms", re.IGNORECASE)
# Windows ping.exe ignores LC_ALL and localizes "time" (German "Zeit<1ms",
# French "temps=4 ms"). An echo reply is recognized by a "TTL=" field (IPv4)
# or by coming from the pinged address (IPv6 replies carry no TTL); error
# replies such as "Destination host unreachable" come from a router address,
# carry no TTL and no "N ms" value.
_LOCALIZED_RTT_RE = re.compile(r"[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*ms\b", re.IGNORECASE)
# An address followed by a colon ("Reply from 2001:db8::1: ...",
# "Réponse de 192.0.2.1 : ..."); the greedy match backtracks to the last colon.
_SOURCE_RE = re.compile(r"(?<![0-9A-Za-z:.%])([0-9A-Fa-f:.]+(?:%[0-9A-Za-z]+)?)\s*:(?:\s|$)")
_TTL_RE = re.compile(r"\bttl\s*=\s*[0-9]+", re.IGNORECASE)
_PERMISSION_MARKERS = (
    "operation not permitted",
    "permission denied",
    "must be root",
    "must run as root",
    "requires root",
    "access denied",
)

PERMISSION_HINT = (
    "The system ping binary lacks the privilege to send ICMP echo requests. "
    "On Linux grant it CAP_NET_RAW (setcap cap_net_raw+ep $(command -v ping)) "
    "or allow unprivileged ICMP via sysctl net.ipv4.ping_group_range; on "
    "macOS/BSD ping must be setuid root (the default)."
)


@dataclass
class ProcessOutput:
    """Captured output of one ``ping`` invocation."""

    returncode: int
    stdout: str
    stderr: str


#: Runs a command with a hard timeout. Raises ``TimeoutError`` when the
#: timeout expires (the process must be killed), ``OSError`` when it cannot be
#: started.
RunProcess = Callable[[Sequence[str], float], Awaitable[ProcessOutput]]

#: Resolves a hostname to a list of IP address strings. Raises
#: ``socket.gaierror`` / ``OSError`` on resolution failure.
Resolver = Callable[[str], Awaitable[list[str]]]

Sleep = Callable[[float], Awaitable[None]]


async def run_process(command: Sequence[str], timeout: float) -> ProcessOutput:
    """Run ``command`` and capture its output, killing it after ``timeout``."""
    env = dict(os.environ)
    # Parseable, untranslated output ("time=1.23 ms").
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    with anyio.fail_after(timeout):
        completed = await anyio.run_process(
            list(command),
            stdin=subprocess.DEVNULL,
            check=False,
            env=env,
        )
    return ProcessOutput(
        returncode=completed.returncode,
        stdout=completed.stdout.decode(errors="replace"),
        stderr=completed.stderr.decode(errors="replace"),
    )


async def resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to IP addresses in resolver order, without duplicates."""
    infos = await anyio.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
    addresses: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def find_binary(name: str, fallbacks: Sequence[str]) -> Optional[str]:
    """Locate a binary on ``PATH`` or at one of the ``fallbacks``."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def build_ping_command(
    platform: str,
    address: str,
    timeout: float,
    ping_binary: str,
    ping6_binary: Optional[str],
) -> list[str]:
    """Build a single-echo ``ping`` command line for ``platform``.

    Args:
        platform: ``sys.platform`` style identifier.
        address: Literal IPv4/IPv6 address to ping (never a hostname, so the
            binary performs no DNS lookup and no option injection is possible).
        timeout: Per-attempt reply timeout in seconds.
        ping_binary: Path of the ``ping`` binary.
        ping6_binary: Path of ``ping6`` where IPv6 needs a separate binary.
    """
    is_ipv6 = ipaddress.ip_address(address.split("%", 1)[0]).version == 6
    wait_ms = str(max(1, math.ceil(timeout * 1000)))

    if platform.startswith("win"):
        command = [ping_binary, "-n", "1", "-w", wait_ms]
        if is_ipv6:
            command.append("-6")
        return [*command, address]

    if platform.startswith("linux"):
        # iputils/busybox: -W is the reply wait in whole seconds.
        wait_s = str(max(1, math.ceil(timeout)))
        return [ping_binary, "-n", "-c", "1", "-W", wait_s, address]

    if is_ipv6:
        # macOS/BSD use ping6, whose wait flag differs between systems, so its
        # wait is only enforced by the process timeout.
        return [ping6_binary or "ping6", "-n", "-c", "1", address]
    if platform.startswith(("darwin", "freebsd")):
        # macOS and FreeBSD: -W is the reply wait in milliseconds.
        return [ping_binary, "-n", "-c", "1", "-W", wait_ms, address]
    # Other systems (OpenBSD, NetBSD, ...) spell the wait flag differently;
    # rely on the process timeout instead of guessing.
    return [ping_binary, "-n", "-c", "1", address]


def _parse_ip(
    value: str,
) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse an IP literal, ignoring an IPv6 zone; ``None`` if it is not one."""
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


def _names_source(
    line: str, target: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> bool:
    """Return whether ``line`` names ``target`` (in any spelling) as a source."""
    return any(
        _parse_ip(match.group(1)) == target for match in _SOURCE_RE.finditer(line)
    )


def parse_rtt_ms(output: str, address: Optional[str] = None) -> Optional[float]:
    """Extract the round-trip time of the first reply from ``ping`` output.

    Args:
        output: Standard output of one ``ping`` run.
        address: The pinged IP address; lets localized Windows IPv6 replies,
            which carry no ``TTL=`` field, be recognized by their source.
    """
    match = _RTT_RE.search(output)
    if match:
        return float(match.group(1).replace(",", "."))
    target = _parse_ip(address) if address else None
    for line in output.splitlines():
        if _TTL_RE.search(line) or (
            target is not None and _names_source(line, target)
        ):
            localized = _LOCALIZED_RTT_RE.search(line)
            if localized:
                return float(localized.group(1).replace(",", "."))
    return None


def is_permission_failure(output: str) -> bool:
    """Return whether ``ping`` output reports missing ICMP privileges."""
    lowered = output.lower()
    return any(marker in lowered for marker in _PERMISSION_MARKERS)


def _error_detail(output: ProcessOutput) -> str:
    """Pick the most useful line of failure output."""
    for text in (output.stderr, output.stdout):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        interesting = [
            line
            for line in lines
            if any(
                word in line.lower()
                for word in (
                    "unreachable",
                    "unknown",
                    "error",
                    "denied",
                    "permitted",
                    "no route",
                    "cannot",
                    "failed",
                    "exceeded",
                )
            )
        ]
        if interesting:
            return interesting[0][:MAX_ERROR_DETAIL]
        if text is output.stderr and lines:
            return lines[-1][:MAX_ERROR_DETAIL]
    return ""


class PingCheckExecutor:
    """Executor for ICMP ping checks using the system ``ping`` binary."""

    def __init__(
        self,
        *,
        run: RunProcess | None = None,
        resolver: Resolver | None = None,
        sleep: Sleep | None = None,
        platform: str | None = None,
        ping_binary: str | None = None,
        ping6_binary: str | None = None,
    ) -> None:
        self._run = run or run_process
        self._resolve = resolver or resolve_host
        self._sleep = sleep or anyio.sleep
        self._platform = platform or sys.platform
        self._ping_binary = ping_binary
        self._ping6_binary = ping6_binary

    async def execute(self, check: Check) -> Result:
        """Execute a ping check and return a Result."""
        try:
            config = PingCheckConfig.from_dict(check.data)
            config.validate()
        except ValueError as exc:
            return self._error(check.check_id, "configuration_error", str(exc), {})

        host = self._target_host(config, check.url)
        if host is None:
            return self._error(
                check.check_id,
                "configuration_error",
                "a host name or IP address is required (set check.url or data.host)",
                {},
            )

        base: dict[str, Any] = {
            "host": host,
            "timeout": config.timeout,
            "count": config.count,
            "interval": config.interval,
        }

        address, resolution_error = await self._resolve_target(host, config.timeout)
        if address is None:
            error_type, message = resolution_error
            return self._error(check.check_id, error_type, message, base)
        base["target"] = address
        if address != host:
            base["hostname"] = host

        ping_binary = self._ping_binary or find_binary("ping", FALLBACK_PING_PATHS)
        if ping_binary is None:
            return self._error(
                check.check_id,
                "ping_unavailable",
                "No 'ping' binary found on PATH or in /sbin, /bin, /usr/bin, /usr/sbin",
                base,
            )
        ping6_binary = self._ping6_binary or find_binary("ping6", FALLBACK_PING6_PATHS)

        command = build_ping_command(
            self._platform, address, config.timeout, ping_binary, ping6_binary
        )
        return await self._run_attempts(check.check_id, command, config, base)

    async def _run_attempts(
        self,
        check_id: int,
        command: list[str],
        config: PingCheckConfig,
        base: dict[str, Any],
    ) -> Result:
        """Send ``config.count`` single-echo attempts and summarise them."""
        attempts: list[dict[str, Any]] = []
        rtts: list[float] = []
        last_error: tuple[str, str] | None = None

        for attempt in range(1, config.count + 1):
            if attempt > 1 and config.interval > 0:
                await self._sleep(config.interval)

            record, failure = await self._attempt(
                attempt, command, config.timeout, base["target"]
            )
            attempts.append(record)
            if "rtt_ms" in record:
                rtts.append(record["rtt_ms"])
                continue

            assert failure is not None
            last_error = failure
            if failure[0] in {"permission_error", "ping_unavailable"}:
                # Retrying cannot help; report the actionable cause right away.
                data = {
                    **base,
                    **self._packet_stats(len(attempts), 0),
                    "attempts": attempts,
                }
                return self._error(check_id, failure[0], failure[1], data)

        data = {
            **base,
            **self._packet_stats(len(attempts), len(rtts)),
            "attempts": attempts,
        }
        if rtts:
            data.update(
                {
                    "rtt_min_ms": min(rtts),
                    "rtt_max_ms": max(rtts),
                    "rtt_avg_ms": round(sum(rtts) / len(rtts), 3),
                    "rtt_list_ms": rtts,
                }
            )
            return Result(check_id=check_id, status=ResultStatus.OK, data=data)

        assert last_error is not None
        error_type, message = last_error
        if all(a["status"] == "timeout" for a in attempts):
            error_type = "timeout"
            message = (
                f"No reply from {base['target']} within {config.timeout:g}s "
                f"({len(attempts)} attempt(s))"
            )
        return self._error(check_id, error_type, message, data)

    async def _attempt(
        self, attempt: int, command: list[str], timeout: float, address: str
    ) -> tuple[dict[str, Any], tuple[str, str] | None]:
        """Run one echo request; return its record and failure, if any."""
        try:
            output = await self._run(command, timeout + PROCESS_GRACE_SECONDS)
        except TimeoutError:
            message = f"No reply within {timeout:g}s"
            return (
                {"attempt": attempt, "status": "timeout", "error_msg": message},
                ("timeout", message),
            )
        except FileNotFoundError as exc:
            message = f"ping binary not found: {exc}"
            return (
                {"attempt": attempt, "status": "error", "error_msg": message},
                ("ping_unavailable", message),
            )
        except PermissionError as exc:
            message = f"{PERMISSION_HINT} ({exc})"
            return (
                {"attempt": attempt, "status": "error", "error_msg": message},
                ("permission_error", message),
            )
        except OSError as exc:
            message = f"Could not run ping: {exc}"
            return (
                {"attempt": attempt, "status": "error", "error_msg": message},
                ("execution_error", message),
            )

        combined = f"{output.stdout}\n{output.stderr}"
        rtt = parse_rtt_ms(output.stdout, address) if output.returncode == 0 else None
        if rtt is not None:
            return {"attempt": attempt, "status": "ok", "rtt_ms": rtt}, None

        detail = _error_detail(output)
        if is_permission_failure(combined):
            message = f"{PERMISSION_HINT} ({detail})" if detail else PERMISSION_HINT
            return (
                {"attempt": attempt, "status": "error", "error_msg": message},
                ("permission_error", message),
            )

        message = detail or f"No reply within {timeout:g}s"
        status = "unreachable" if detail else "timeout"
        record: dict[str, Any] = {
            "attempt": attempt,
            "status": status,
            "error_msg": message,
            "returncode": output.returncode,
        }
        return record, ("unreachable" if detail else "timeout", message)

    async def _resolve_target(
        self, host: str, timeout: float
    ) -> tuple[Optional[str], tuple[str, str]]:
        """Resolve ``host`` to one IP address (literal addresses pass through)."""
        try:
            ipaddress.ip_address(host.split("%", 1)[0])
            return host, ("", "")
        except ValueError:
            pass

        try:
            with anyio.fail_after(timeout):
                addresses = await self._resolve(host)
        except TimeoutError:
            return None, (
                "dns_timeout",
                f"Resolving {host} timed out after {timeout:g}s",
            )
        except (socket.gaierror, OSError, UnicodeError) as exc:
            return None, ("dns_error", f"Could not resolve {host}: {exc}")

        if not addresses:
            return None, ("dns_error", f"Could not resolve {host}: no addresses")
        return addresses[0], ("", "")

    @staticmethod
    def _target_host(config: PingCheckConfig, url: str) -> Optional[str]:
        """Determine the host to ping from config or the check URL."""
        return normalize_ping_target(config.host or url)

    @staticmethod
    def _packet_stats(sent: int, received: int) -> dict[str, Any]:
        loss = 0.0 if sent == 0 else round((sent - received) * 100.0 / sent, 1)
        return {
            "packets_sent": sent,
            "packets_received": received,
            "packet_loss_percent": loss,
        }

    @staticmethod
    def _error(
        check_id: int, error_type: str, message: str, extra: dict[str, Any]
    ) -> Result:
        """Build an error Result with standard fields."""
        data: dict[str, Any] = {"error_type": error_type, "error_msg": message}
        data.update(extra)
        return Result(check_id=check_id, status=ResultStatus.ERROR, data=data)

    async def aclose(self) -> None:
        """Executor cleanup hook (each attempt's process is reaped on return)."""
        return
