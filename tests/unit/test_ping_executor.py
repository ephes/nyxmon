"""Unit tests for the ping check executor.

No test sends ICMP or touches the network: the process runner, resolver and
sleep are all injected stubs.
"""

from __future__ import annotations

import logging
import socket
import sys
from typing import Any, Sequence

import anyio
import pytest
from anyio.from_thread import BlockingPortalProvider

from nyxmon.adapters.collector import estimated_check_runtime_seconds
from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.adapters.runner import AsyncCheckRunner
from nyxmon.adapters.runner.executors import ping_executor as ping_module
from nyxmon.adapters.runner.executors.ping_executor import (
    PROCESS_GRACE_SECONDS,
    PingCheckExecutor,
    ProcessOutput,
    build_ping_command,
    parse_rtt_ms,
    run_process,
)
from nyxmon.domain import Check, CheckType, PingCheckConfig, ResultStatus
from nyxmon.service_layer import UnitOfWork
from nyxmon.startup_validation import validate_check_types


pytestmark = pytest.mark.anyio

LINUX_REPLY = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.
64 bytes from 192.0.2.1: icmp_seq=1 ttl=64 time={rtt} ms

--- 192.0.2.1 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
rtt min/avg/max/mdev = {rtt}/{rtt}/{rtt}/0.000 ms
"""

LINUX_NO_REPLY = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.

--- 192.0.2.1 ping statistics ---
1 packets transmitted, 0 received, 100% packet loss, time 0ms
"""

LINUX_UNREACHABLE = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.
From 192.0.2.254 icmp_seq=1 Destination Host Unreachable

--- 192.0.2.1 ping statistics ---
1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms
"""

MACOS_REPLY = """PING 192.0.2.1 (192.0.2.1): 56 data bytes
64 bytes from 192.0.2.1: icmp_seq=0 ttl=64 time=3.512 ms

--- 192.0.2.1 ping statistics ---
1 packets transmitted, 1 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 3.512/3.512/3.512/0.000 ms
"""

WINDOWS_REPLY = """Pinging 192.0.2.1 with 32 bytes of data:
Reply from 192.0.2.1: bytes=32 time<1ms TTL=64
"""

WINDOWS_REPLY_DE = """Ping wird ausgef\u00fchrt f\u00fcr 192.0.2.1 mit 32 Bytes Daten:
Antwort von 192.0.2.1: Bytes=32 Zeit<1ms TTL=64
"""

WINDOWS_REPLY_FR = """Envoi d'une requ\u00eate 'Ping'  192.0.2.1 avec 32 octets de donn\u00e9es :
R\u00e9ponse de 192.0.2.1 : octets=32 temps=4 ms TTL=64
"""

WINDOWS_UNREACHABLE_DE = """Ping wird ausgef\u00fchrt f\u00fcr 192.0.2.1 mit 32 Bytes Daten:
Antwort von 192.0.2.254: Zielhost nicht erreichbar.
"""

WINDOWS_REPLY_V6_DE = """Ping wird ausgef\u00fchrt f\u00fcr 2001:db8::1 mit 32 Bytes Daten:
Antwort von 2001:db8::1: Zeit<1ms
"""

WINDOWS_UNREACHABLE_V6_DE = """Ping wird ausgef\u00fchrt f\u00fcr 2001:db8::1 mit 32 Bytes Daten:
Antwort von 2001:db8::fe: Zielhost nicht erreichbar.
"""

WINDOWS_ROUTER_UNREACHABLE = """Pinging 192.0.2.1 with 32 bytes of data:
Reply from 192.0.2.254: Destination host unreachable.
"""


def _reply(rtt: float) -> ProcessOutput:
    return ProcessOutput(0, LINUX_REPLY.format(rtt=rtt), "")


NO_REPLY = ProcessOutput(1, LINUX_NO_REPLY, "")


class StubRunner:
    """Replays scripted process outcomes and records the commands."""

    def __init__(self, *outcomes: ProcessOutput | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[str], float]] = []

    async def __call__(self, command: Sequence[str], timeout: float) -> ProcessOutput:
        self.calls.append((list(command), timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class StubResolver:
    def __init__(
        self, addresses: list[str] | None = None, exc: Exception | None = None
    ) -> None:
        self.addresses = addresses or []
        self.exc = exc
        self.calls: list[str] = []

    async def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        if self.exc is not None:
            raise self.exc
        return self.addresses


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _check(url: str = "192.0.2.1", **data: Any) -> Check:
    return Check(
        check_id=7,
        service_id=1,
        name="Gateway ping",
        check_type=CheckType.PING,
        url=url,
        data=data,
    )


def _executor(
    runner: StubRunner,
    resolver: StubResolver | None = None,
    sleep: RecordingSleep | None = None,
    platform: str = "linux",
) -> PingCheckExecutor:
    return PingCheckExecutor(
        run=runner,
        resolver=resolver or StubResolver(exc=AssertionError("no DNS expected")),
        sleep=sleep or RecordingSleep(),
        platform=platform,
        ping_binary="/bin/ping",
        ping6_binary="/sbin/ping6",
    )


class TestSuccessAndLoss:
    async def test_all_replies_ok_with_rtt_stats(self) -> None:
        runner = StubRunner(_reply(1.5), _reply(0.5), _reply(1.0))
        sleep = RecordingSleep()

        result = await _executor(runner, sleep=sleep).execute(_check())

        assert result.status == ResultStatus.OK
        assert result.check_id == 7
        data = result.data
        assert data["target"] == "192.0.2.1"
        assert "hostname" not in data
        assert data["packets_sent"] == 3
        assert data["packets_received"] == 3
        assert data["packet_loss_percent"] == 0.0
        assert data["rtt_list_ms"] == [1.5, 0.5, 1.0]
        assert data["rtt_min_ms"] == 0.5
        assert data["rtt_max_ms"] == 1.5
        assert data["rtt_avg_ms"] == 1.0
        assert [a["status"] for a in data["attempts"]] == ["ok", "ok", "ok"]
        # Default interval between (not after) attempts.
        assert sleep.calls == [1.0, 1.0]
        # Per-attempt timeout plus process grace.
        assert {timeout for _, timeout in runner.calls} == {5.0 + PROCESS_GRACE_SECONDS}
        assert runner.calls[0][0] == [
            "/bin/ping",
            "-n",
            "-c",
            "1",
            "-W",
            "5",
            "192.0.2.1",
        ]

    async def test_partial_loss_still_ok(self) -> None:
        runner = StubRunner(NO_REPLY, _reply(2.0), TimeoutError())

        result = await _executor(runner).execute(_check())

        assert result.status == ResultStatus.OK
        assert result.data["packets_sent"] == 3
        assert result.data["packets_received"] == 1
        assert result.data["packet_loss_percent"] == 66.7
        assert result.data["rtt_list_ms"] == [2.0]
        assert [a["status"] for a in result.data["attempts"]] == [
            "timeout",
            "ok",
            "timeout",
        ]

    async def test_count_and_interval_from_config(self) -> None:
        runner = StubRunner(_reply(1.0), _reply(1.0))
        sleep = RecordingSleep()

        result = await _executor(runner, sleep=sleep).execute(
            _check(count=2, interval=0.25, timeout=1.5)
        )

        assert result.status == ResultStatus.OK
        assert len(runner.calls) == 2
        assert sleep.calls == [0.25]
        assert runner.calls[0][0][5] == "2"  # Linux -W rounds up to whole seconds
        assert runner.calls[0][1] == 1.5 + PROCESS_GRACE_SECONDS


class TestFailures:
    async def test_all_timeouts_is_error(self) -> None:
        runner = StubRunner(TimeoutError(), NO_REPLY, TimeoutError())

        result = await _executor(runner).execute(_check())

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "timeout"
        assert "No reply from 192.0.2.1 within 5s" in result.data["error_msg"]
        assert result.data["packets_sent"] == 3
        assert result.data["packets_received"] == 0
        assert result.data["packet_loss_percent"] == 100.0
        assert "rtt_list_ms" not in result.data
        assert len(result.data["attempts"]) == 3

    async def test_unreachable_reports_ping_message(self) -> None:
        unreachable = ProcessOutput(1, LINUX_UNREACHABLE, "")
        runner = StubRunner(unreachable, unreachable, unreachable)

        result = await _executor(runner).execute(_check())

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "unreachable"
        assert "Destination Host Unreachable" in result.data["error_msg"]
        assert result.data["packet_loss_percent"] == 100.0

    async def test_permission_error_from_ping_output_stops_attempts(self) -> None:
        denied = ProcessOutput(2, "", "ping: socket: Operation not permitted\n")
        runner = StubRunner(denied, _reply(1.0), _reply(1.0))

        result = await _executor(runner).execute(_check())

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "permission_error"
        assert "CAP_NET_RAW" in result.data["error_msg"]
        assert "setuid" in result.data["error_msg"]
        assert "Operation not permitted" in result.data["error_msg"]
        assert len(runner.calls) == 1
        assert result.data["packets_received"] == 0

    async def test_permission_error_starting_binary(self) -> None:
        runner = StubRunner(PermissionError(13, "Permission denied"))

        result = await _executor(runner).execute(_check())

        assert result.data["error_type"] == "permission_error"
        assert "CAP_NET_RAW" in result.data["error_msg"]

    async def test_missing_binary_is_reported(self) -> None:
        runner = StubRunner(FileNotFoundError(2, "No such file", "/bin/ping"))

        result = await _executor(runner).execute(_check())

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "ping_unavailable"
        assert len(runner.calls) == 1

    async def test_no_binary_found(self, monkeypatch) -> None:
        monkeypatch.setattr(ping_module, "find_binary", lambda name, fallbacks: None)
        runner = StubRunner()
        executor = PingCheckExecutor(
            run=runner, resolver=StubResolver(), sleep=RecordingSleep()
        )

        result = await executor.execute(_check())

        assert result.data["error_type"] == "ping_unavailable"
        assert runner.calls == []

    async def test_windows_router_unreachable_exit_zero_is_not_success(self) -> None:
        reply = ProcessOutput(0, WINDOWS_ROUTER_UNREACHABLE, "")
        runner = StubRunner(reply)

        result = await _executor(runner, platform="win32").execute(_check(count=1))

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "unreachable"

    async def test_localized_windows_reply_is_success(self) -> None:
        runner = StubRunner(
            ProcessOutput(0, WINDOWS_REPLY_DE, ""),
            ProcessOutput(0, WINDOWS_UNREACHABLE_DE, ""),
        )

        result = await _executor(runner, platform="win32").execute(_check(count=2))

        assert result.status == ResultStatus.OK
        assert result.data["packets_received"] == 1
        assert result.data["rtt_list_ms"] == [1.0]
        assert result.data["attempts"][1]["status"] != "ok"

    async def test_localized_windows_ipv6_reply_is_success(self) -> None:
        runner = StubRunner(
            ProcessOutput(0, WINDOWS_UNREACHABLE_V6_DE, ""),
            ProcessOutput(0, WINDOWS_REPLY_V6_DE, ""),
        )

        result = await _executor(runner, platform="win32").execute(
            _check("2001:db8::1", count=2)
        )

        assert result.status == ResultStatus.OK
        assert result.data["packets_received"] == 1
        assert result.data["rtt_list_ms"] == [1.0]
        assert runner.calls[0][0][-2:] == ["-6", "2001:db8::1"]


class TestResolution:
    async def test_hostname_resolved_before_ping(self) -> None:
        resolver = StubResolver(["192.0.2.10", "2001:db8::10"])
        runner = StubRunner(_reply(1.0))

        result = await _executor(runner, resolver).execute(
            _check("gateway.example.test", count=1)
        )

        assert result.status == ResultStatus.OK
        assert resolver.calls == ["gateway.example.test"]
        assert result.data["target"] == "192.0.2.10"
        assert result.data["hostname"] == "gateway.example.test"
        assert runner.calls[0][0][-1] == "192.0.2.10"

    async def test_dns_failure_is_error_without_ping(self) -> None:
        resolver = StubResolver(exc=socket.gaierror(8, "nodename nor servname"))
        runner = StubRunner()

        result = await _executor(runner, resolver).execute(_check("nas.invalid"))

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "dns_error"
        assert "nas.invalid" in result.data["error_msg"]
        assert runner.calls == []

    async def test_empty_resolution_is_error(self) -> None:
        result = await _executor(StubRunner(), StubResolver([])).execute(
            _check("nas.example.test")
        )

        assert result.data["error_type"] == "dns_error"

    async def test_dns_timeout_is_error(self) -> None:
        class SlowResolver:
            async def __call__(self, host: str) -> list[str]:
                await anyio.sleep(10)
                return ["192.0.2.1"]

        executor = PingCheckExecutor(
            run=StubRunner(),
            resolver=SlowResolver(),
            sleep=RecordingSleep(),
            platform="linux",
            ping_binary="/bin/ping",
        )

        result = await executor.execute(_check("slow.example.test", timeout=0.05))

        assert result.data["error_type"] == "dns_timeout"

    @pytest.mark.parametrize(
        ("url", "data", "expected"),
        [
            ("https://router.example.test:8443/status", {}, "router.example.test"),
            ("[2001:db8::1]", {}, "2001:db8::1"),
            ("ignored.example.test", {"host": "192.0.2.5"}, "192.0.2.5"),
        ],
    )
    async def test_target_extraction(self, url, data, expected) -> None:
        resolver = StubResolver(["192.0.2.99"])
        runner = StubRunner(_reply(1.0))

        await _executor(runner, resolver, platform="linux").execute(
            _check(url, count=1, **data)
        )

        resolved = resolver.calls[0] if resolver.calls else runner.calls[0][0][-1]
        assert resolved == expected

    @pytest.mark.parametrize("url", ["", "   ", "-f", "host name"])
    async def test_invalid_target_is_configuration_error(self, url) -> None:
        runner = StubRunner()

        result = await _executor(runner).execute(_check(url))

        assert result.data["error_type"] == "configuration_error"
        assert runner.calls == []


class TestConfiguration:
    @pytest.mark.parametrize(
        "data",
        [
            {"timeout": 0},
            {"timeout": "soon"},
            {"timeout": True},
            {"timeout": float("nan")},
            {"timeout": 61},
            {"count": 0},
            {"count": 21},
            {"count": 1.5},
            {"interval": -1},
            {"host": 42},
        ],
    )
    async def test_invalid_config_is_configuration_error(self, data) -> None:
        runner = StubRunner()

        result = await _executor(runner).execute(_check(**data))

        assert result.status == ResultStatus.ERROR
        assert result.data["error_type"] == "configuration_error"
        assert runner.calls == []

    def test_defaults_and_round_trip(self) -> None:
        config = PingCheckConfig.from_dict({})
        assert (config.timeout, config.count, config.interval, config.host) == (
            5.0,
            3,
            1.0,
            None,
        )
        config = PingCheckConfig.from_dict(
            {"timeout": "2", "count": "4", "interval": 0, "host": " nas "}
        )
        assert config.to_dict() == {
            "timeout": 2.0,
            "count": 4,
            "interval": 0.0,
            "host": "nas",
        }


class TestCommandsAndParsing:
    def test_linux_command(self) -> None:
        assert build_ping_command("linux", "2001:db8::1", 0.2, "/bin/ping", None) == [
            "/bin/ping",
            "-n",
            "-c",
            "1",
            "-W",
            "1",
            "2001:db8::1",
        ]

    def test_macos_commands(self) -> None:
        assert build_ping_command(
            "darwin", "192.0.2.1", 2.5, "/sbin/ping", "/sbin/ping6"
        ) == ["/sbin/ping", "-n", "-c", "1", "-W", "2500", "192.0.2.1"]
        assert build_ping_command(
            "darwin", "fe80::1%en0", 2.5, "/sbin/ping", "/sbin/ping6"
        ) == ["/sbin/ping6", "-n", "-c", "1", "fe80::1%en0"]

    @pytest.mark.parametrize("platform", ["openbsd7", "netbsd10"])
    def test_other_bsd_command_has_no_wait_flag(self, platform) -> None:
        assert build_ping_command(platform, "192.0.2.1", 2, "/sbin/ping", None) == [
            "/sbin/ping",
            "-n",
            "-c",
            "1",
            "192.0.2.1",
        ]

    def test_freebsd_command_uses_millisecond_wait(self) -> None:
        assert build_ping_command(
            "freebsd14", "192.0.2.1", 1, "/sbin/ping", "/sbin/ping6"
        ) == ["/sbin/ping", "-n", "-c", "1", "-W", "1000", "192.0.2.1"]

    def test_windows_command(self) -> None:
        assert build_ping_command("win32", "2001:db8::1", 1, "ping", None) == [
            "ping",
            "-n",
            "1",
            "-w",
            "1000",
            "-6",
            "2001:db8::1",
        ]

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            (LINUX_REPLY.format(rtt=0.042), 0.042),
            (MACOS_REPLY, 3.512),
            (WINDOWS_REPLY, 1.0),
            (WINDOWS_REPLY_DE, 1.0),
            (WINDOWS_REPLY_FR, 4.0),
            (WINDOWS_UNREACHABLE_DE, None),
            (WINDOWS_ROUTER_UNREACHABLE, None),
            ("64 bytes from x: icmp_seq=1 time=1,25 ms", 1.25),
            (LINUX_NO_REPLY, None),
        ],
    )
    def test_parse_rtt(self, output, expected) -> None:
        assert parse_rtt_ms(output) == expected

    def test_parse_localized_ipv6_reply_by_source_address(self) -> None:
        assert parse_rtt_ms(WINDOWS_REPLY_V6_DE, "2001:db8::1") == 1.0
        # Without the target address there is nothing marking it as a reply.
        assert parse_rtt_ms(WINDOWS_REPLY_V6_DE) is None
        # A reply from another address (router) is not an echo reply.
        assert parse_rtt_ms(WINDOWS_REPLY_V6_DE, "2001:db8::") is None
        assert parse_rtt_ms(WINDOWS_UNREACHABLE_V6_DE, "2001:db8::1") is None
        # Equivalent spellings of the target match the canonical reply source.
        assert (
            parse_rtt_ms(WINDOWS_REPLY_V6_DE, "2001:0db8:0000:0000:0000:0000:0000:0001")
            == 1.0
        )
        assert parse_rtt_ms(WINDOWS_REPLY_V6_DE, "2001:DB8::1") == 1.0
        assert parse_rtt_ms(WINDOWS_REPLY_FR.replace(" TTL=64", ""), "192.0.2.1") == 4.0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell helper")
    async def test_run_process_kills_after_timeout(self) -> None:
        """The default runner enforces its timeout on a real (non-ping) process."""
        with pytest.raises(TimeoutError):
            await run_process(["sleep", "5"], 0.1)

        output = await run_process(["sh", "-c", "echo out; echo err >&2; exit 3"], 5)
        assert output == ProcessOutput(3, "out\n", "err\n")


class TestRegistration:
    def test_runner_registers_ping_executor(self) -> None:
        runner = AsyncCheckRunner(BlockingPortalProvider())
        assert isinstance(
            runner.executor_registry.get_executor(CheckType.PING), PingCheckExecutor
        )
        runner._register_executors(None)
        assert isinstance(
            runner.executor_registry.get_executor(CheckType.PING), PingCheckExecutor
        )

    async def test_startup_validation_accepts_ping_checks(self, caplog) -> None:
        uow = UnitOfWork(store=InMemoryStore())
        with uow:
            uow.store.checks.add(_check())
            uow.commit()

        with caplog.at_level(logging.INFO):
            await validate_check_types(uow, AsyncCheckRunner(BlockingPortalProvider()))

        assert "all types registered" in caplog.text
        assert "WARNING" not in caplog.text

    def test_runtime_estimate_covers_worst_case(self) -> None:
        # Defaults: 3 attempts + DNS at (5s + 2s grace), 2 intervals, 30s slack.
        assert estimated_check_runtime_seconds(_check()) == 4 * 7 + 2 + 30
        assert (
            estimated_check_runtime_seconds(_check(count=10, timeout=10, interval=2))
            == 11 * 12 + 9 * 2 + 30
        )
