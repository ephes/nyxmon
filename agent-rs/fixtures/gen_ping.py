"""Fixture builders of slice S6 part 3 (ping), loaded by ``generate.py``.

* ``config_ping.json``: ``PingCheckConfig.from_dict`` + ``validate`` and the
  target host (``normalize_ping_target``) as ``PingCheckExecutor.execute``
  runs them before the name resolution.
* ``ping_text.json``: ``build_ping_command``, ``parse_rtt_ms``,
  ``is_permission_failure``, ``_error_detail``, ``_packet_stats``, the RTT
  statistics (``min``/``max``/``round(sum(...) / n, 3)``), the output decoding
  of ``run_process`` and scripted executor runs (stub process runner,
  resolver and sleep).
* ``ping_unicode.json``: the ``re`` and ``str`` character classes the ping
  parsers depend on (``\\w``, ``\\s``, the ``IGNORECASE`` equivalents of the
  pattern letters, ``str.splitlines`` separators). ``nyxmon_core::ping_text``
  embeds ``word`` and checks the others in its tests.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable, Mapping
from typing import Any
from unittest import mock


def _run_async(func: Callable[[], Any]) -> Any:
    import anyio

    return anyio.run(func)


# ------------------------------------------------------------ configuration

#: (check.data as JSON text, check.url) pairs. A JSON ``null`` data value is
#: not in the corpus: the store reads it as ``{}`` (PLAN.md D-11).
PING_CONFIG_CORPUS: list[tuple[str, str]] = [
    ("{}", "192.0.2.1"),
    ("{}", ""),
    ("{}", "   "),
    ("{}", "-f"),
    ("{}", "host name"),
    ("{}", "host\tname"),
    ("{}", "\u00a0host"),
    ("{}", "host\u2028"),
    ("{}", "host\x1c"),
    ("{}", " nas.local "),
    ("{}", "https://router.example.test:8443/status"),
    ("{}", "HTTPS://Router.Example/"),
    ("{}", "https://[2001:db8::1]:443/"),
    ("{}", "https://[fe80::1%25en0]/"),
    ("{}", "https://[::1"),
    ("{}", "https://"),
    ("{}", "https://user@/x"),
    ("{}", "x://-host/"),
    ("{}", "x:// spaced/"),
    ("{}", "[2001:db8::1]"),
    ("{}", "[ ]"),
    ("{}", "[-x]"),
    ("{}", "[]"),
    ("{}", "[2001:db8::1"),
    ("{}", "fe80::1%en0"),
    ("{}", "192.0.2.1%x"),
    ("{}", "-"),
    ("{}", "a\u3000b"),
    ("{}", "gateway.example.test"),
    ('{"host": "192.0.2.5"}', "ignored.example.test"),
    ('{"host": " nas "}', ""),
    ('{"host": ""}', "fallback.example"),
    ('{"host": "   "}', "fallback.example"),
    ('{"host": null}', "fallback.example"),
    ('{"host": 42}', "192.0.2.1"),
    ('{"host": true}', "192.0.2.1"),
    ('{"host": ["a"]}', "192.0.2.1"),
    ('{"host": {}}', "192.0.2.1"),
    ('{"host": "-x"}', "fallback.example"),
    ('{"host": "https://h.example/"}', ""),
    ('{"host": "[2001:db8::2]"}', ""),
    ('{"timeout": 0}', "192.0.2.1"),
    ('{"timeout": 0.0001}', "192.0.2.1"),
    ('{"timeout": -1}', "192.0.2.1"),
    ('{"timeout": -0.0}', "192.0.2.1"),
    ('{"timeout": 60}', "192.0.2.1"),
    ('{"timeout": 60.0001}', "192.0.2.1"),
    ('{"timeout": 61}', "192.0.2.1"),
    ('{"timeout": "2"}', "192.0.2.1"),
    ('{"timeout": " 2.5 "}', "192.0.2.1"),
    ('{"timeout": "1_0"}', "192.0.2.1"),
    ('{"timeout": "1__0"}', "192.0.2.1"),
    ('{"timeout": "soon"}', "192.0.2.1"),
    ('{"timeout": ""}', "192.0.2.1"),
    ('{"timeout": "inf"}', "192.0.2.1"),
    ('{"timeout": "-Infinity"}', "192.0.2.1"),
    ('{"timeout": "nan"}', "192.0.2.1"),
    ('{"timeout": "1e400"}', "192.0.2.1"),
    ('{"timeout": "0x10"}', "192.0.2.1"),
    ('{"timeout": NaN}', "192.0.2.1"),
    ('{"timeout": Infinity}', "192.0.2.1"),
    ('{"timeout": true}', "192.0.2.1"),
    ('{"timeout": false}', "192.0.2.1"),
    ('{"timeout": null}', "192.0.2.1"),
    ('{"timeout": [1]}', "192.0.2.1"),
    ('{"timeout": {}}', "192.0.2.1"),
    ('{"timeout": ' + "1" * 400 + "}", "192.0.2.1"),
    ('{"timeout": -' + "1" * 400 + "}", "192.0.2.1"),
    ('{"timeout": 1.5}', "192.0.2.1"),
    ('{"timeout": 1e-320}', "192.0.2.1"),
    ('{"count": 0}', "192.0.2.1"),
    ('{"count": 1}', "192.0.2.1"),
    ('{"count": 20}', "192.0.2.1"),
    ('{"count": 21}', "192.0.2.1"),
    ('{"count": 1.5}', "192.0.2.1"),
    ('{"count": 2.0}', "192.0.2.1"),
    ('{"count": "4"}', "192.0.2.1"),
    ('{"count": "4.0"}', "192.0.2.1"),
    ('{"count": "4.5"}', "192.0.2.1"),
    ('{"count": 1e300}', "192.0.2.1"),
    ('{"count": -0.0}', "192.0.2.1"),
    ('{"count": -1}', "192.0.2.1"),
    ('{"count": true}', "192.0.2.1"),
    ('{"count": "inf"}', "192.0.2.1"),
    ('{"count": ' + "9" * 30 + "}", "192.0.2.1"),
    ('{"count": ' + "9" * 400 + "}", "192.0.2.1"),
    ('{"count": 1.5, "host": 42}', "192.0.2.1"),
    ('{"count": 3, "host": 42, "timeout": "x"}', "192.0.2.1"),
    ('{"timeout": "x", "interval": "y"}', "192.0.2.1"),
    ('{"interval": "y", "host": 1}', "192.0.2.1"),
    ('{"timeout": 99, "count": 99, "interval": 99}', "192.0.2.1"),
    ('{"count": 99, "interval": 99}', "192.0.2.1"),
    ('{"interval": -1}', "192.0.2.1"),
    ('{"interval": -0.0}', "192.0.2.1"),
    ('{"interval": 0}', "192.0.2.1"),
    ('{"interval": 60}', "192.0.2.1"),
    ('{"interval": 60.5}', "192.0.2.1"),
    ('{"interval": "0.25"}', "192.0.2.1"),
    ('{"interval": Infinity}', "192.0.2.1"),
    ('{"timeout": "2", "count": "4", "interval": 0, "host": " nas "}', ""),
    ('{"timeout": 2, "count": 4, "interval": 0.5, "extra": [1]}', "nas"),
    ("[]", "192.0.2.1"),
    ('"x"', "192.0.2.1"),
    ("5", "192.0.2.1"),
    ("true", "192.0.2.1"),
]


def _ping_pre_io(data_json: str, url: str) -> dict[str, Any]:
    """Run ``PingCheckExecutor.execute`` up to the name resolution."""
    from nyxmon.adapters.runner.executors.ping_executor import PingCheckExecutor
    from nyxmon.domain import Check, CheckType

    captured: dict[str, Any] = {}

    async def fake_resolve(
        self: Any, host: str, timeout: float
    ) -> tuple[None, tuple[str, str]]:
        captured["host"] = host
        return None, ("dns_error", "stop")

    check = Check(
        check_id=1,
        service_id=1,
        name="ping",
        check_type=CheckType.PING,
        url=url,
        data=json.loads(data_json),
    )
    with mock.patch.object(PingCheckExecutor, "_resolve_target", fake_resolve):
        try:
            result = _run_async(lambda: PingCheckExecutor().execute(check))
        except Exception as exc:  # noqa: BLE001 - the runner's executor_exception
            return {"kind": "exception", "class": type(exc).__name__}
    outcome: dict[str, Any] = {
        "kind": "ready" if captured else "configuration_error",
        "result_json": json.dumps(result.data),
    }
    if captured:
        outcome["host"] = captured["host"]
    return outcome


def build_config_ping() -> dict[str, Any]:
    """Ping check configuration and the target host."""
    return {
        "cases": [
            {
                "data_json": data_json,
                "url": url,
                "outcome": _ping_pre_io(data_json, url),
            }
            for data_json, url in PING_CONFIG_CORPUS
        ]
    }


# ------------------------------------------------------------ ping output

#: iputils 20240117 (Ubuntu 24.04, macmini), ``LC_ALL=C``.
LINUX_REPLY = (
    "PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.\n"
    "64 bytes from 192.0.2.1: icmp_seq=1 ttl=64 time={rtt} ms\n\n"
    "--- 192.0.2.1 ping statistics ---\n"
    "1 packets transmitted, 1 received, 0% packet loss, time 0ms\n"
    "rtt min/avg/max/mdev = {rtt}/{rtt}/{rtt}/0.000 ms\n"
)
LINUX_REPLY_V6 = (
    "PING ::1 (::1) 56 data bytes\n"
    "64 bytes from ::1: icmp_seq=1 ttl=64 time=0.043 ms\n\n"
    "--- ::1 ping statistics ---\n"
    "1 packets transmitted, 1 received, 0% packet loss, time 0ms\n"
    "rtt min/avg/max/mdev = 0.043/0.043/0.043/0.000 ms\n"
)
LINUX_NO_REPLY = (
    "PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.\n\n"
    "--- 192.0.2.1 ping statistics ---\n"
    "1 packets transmitted, 0 received, 100% packet loss, time 0ms\n\n"
)
LINUX_UNREACHABLE = (
    "PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.\n"
    "From 192.0.2.254 icmp_seq=1 Destination Host Unreachable\n\n"
    "--- 192.0.2.1 ping statistics ---\n"
    "1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms\n\n"
)
LINUX_NO_ROUTE = "ping: connect: Network is unreachable\n"
#: iputils without ``cap_net_raw`` and with a closed ``ping_group_range``.
LINUX_DENIED = (
    "ping: socktype: SOCK_RAW\n"
    "ping: socket: Operation not permitted\n"
    "ping: => missing cap_net_raw+p capability or setuid?\n"
)
#: macOS 15 ``ping``/``ping6``.
MACOS_REPLY = (
    "PING 192.0.2.1 (192.0.2.1): 56 data bytes\n"
    "64 bytes from 192.0.2.1: icmp_seq=0 ttl=64 time=3.512 ms\n\n"
    "--- 192.0.2.1 ping statistics ---\n"
    "1 packets transmitted, 1 packets received, 0.0% packet loss\n"
    "round-trip min/avg/max/stddev = 3.512/3.512/3.512/0.000 ms\n"
)
MACOS_NO_REPLY = (
    "PING 192.0.2.1 (192.0.2.1): 56 data bytes\n\n"
    "--- 192.0.2.1 ping statistics ---\n"
    "1 packets transmitted, 0 packets received, 100.0% packet loss\n"
)
MACOS_PING6_REPLY = (
    "PING6(56=40+8+8 bytes) ::1 --> ::1\n"
    "16 bytes from ::1, icmp_seq=0 hlim=64 time=0.177 ms\n\n"
    "--- ::1 ping6 statistics ---\n"
    "1 packets transmitted, 1 packets received, 0.0% packet loss\n"
    "round-trip min/avg/max/std-dev = 0.177/0.177/0.177/nan ms\n"
)
WINDOWS_REPLY = (
    "Pinging 192.0.2.1 with 32 bytes of data:\n"
    "Reply from 192.0.2.1: bytes=32 time<1ms TTL=64\n"
)
WINDOWS_REPLY_DE = (
    "Ping wird ausgef\u00fchrt f\u00fcr 192.0.2.1 mit 32 Bytes Daten:\n"
    "Antwort von 192.0.2.1: Bytes=32 Zeit<1ms TTL=64\n"
)
WINDOWS_REPLY_FR = (
    "Envoi d'une requ\u00eate 'Ping'  192.0.2.1 avec 32 octets de donn\u00e9es :\n"
    "R\u00e9ponse de 192.0.2.1 : octets=32 temps=4 ms TTL=64\n"
)
WINDOWS_UNREACHABLE_DE = (
    "Ping wird ausgef\u00fchrt f\u00fcr 192.0.2.1 mit 32 Bytes Daten:\n"
    "Antwort von 192.0.2.254: Zielhost nicht erreichbar.\n"
)
WINDOWS_REPLY_V6_DE = (
    "Ping wird ausgef\u00fchrt f\u00fcr 2001:db8::1 mit 32 Bytes Daten:\n"
    "Antwort von 2001:db8::1: Zeit<1ms\n"
)
WINDOWS_UNREACHABLE_V6_DE = (
    "Ping wird ausgef\u00fchrt f\u00fcr 2001:db8::1 mit 32 Bytes Daten:\n"
    "Antwort von 2001:db8::fe: Zielhost nicht erreichbar.\n"
)
WINDOWS_ROUTER_UNREACHABLE = (
    "Pinging 192.0.2.1 with 32 bytes of data:\n"
    "Reply from 192.0.2.254: Destination host unreachable.\n"
)

#: (output, address) pairs for ``parse_rtt_ms``.
RTT_CORPUS: list[tuple[str, str | None]] = [
    (LINUX_REPLY.format(rtt=0.042), None),
    (LINUX_REPLY.format(rtt=12), "192.0.2.1"),
    (LINUX_REPLY.format(rtt=123), "192.0.2.1"),
    (LINUX_REPLY_V6, "::1"),
    (LINUX_NO_REPLY, "192.0.2.1"),
    (LINUX_UNREACHABLE, "192.0.2.1"),
    (MACOS_REPLY, None),
    (MACOS_NO_REPLY, "192.0.2.1"),
    (MACOS_PING6_REPLY, "::1"),
    (WINDOWS_REPLY, None),
    (WINDOWS_REPLY_DE, None),
    (WINDOWS_REPLY_FR, None),
    (WINDOWS_UNREACHABLE_DE, None),
    (WINDOWS_ROUTER_UNREACHABLE, None),
    ("64 bytes from x: icmp_seq=1 time=1,25 ms", None),
    ("time=" + "9" * 400 + " ms", None),
    ("time=00012.50 ms", None),
    (WINDOWS_REPLY_V6_DE, "2001:db8::1"),
    (WINDOWS_REPLY_V6_DE, None),
    (WINDOWS_REPLY_V6_DE, "2001:db8::"),
    (WINDOWS_UNREACHABLE_V6_DE, "2001:db8::1"),
    (WINDOWS_REPLY_V6_DE, "2001:0db8:0000:0000:0000:0000:0000:0001"),
    (WINDOWS_REPLY_V6_DE, "2001:DB8::1"),
    (WINDOWS_REPLY_V6_DE, "2001:db8::1%eth0"),
    (WINDOWS_REPLY_V6_DE, ""),
    (WINDOWS_REPLY_V6_DE, "not an address"),
    (WINDOWS_REPLY_FR.replace(" TTL=64", ""), "192.0.2.1"),
    ("Antwort von fe80::1%12: Zeit=3ms", "fe80::1"),
    ("Antwort von fe80::1%12 : Zeit=3ms", "fe80::1%4"),
    ("Antwort von fe80::1%: Zeit=3ms", "fe80::1"),
    ("Antwort von fe80::1%%1: Zeit=3ms", "fe80::1"),
    ("Antwort von x1.2.3.4: Zeit=3ms", "1.2.3.4"),
    ("Antwort von 1.2.3.4:Zeit=3ms", "1.2.3.4"),
    ("Antwort von 1.2.3.4:", "1.2.3.4"),
    ("Antwort von 1.2.3.4:\n", "1.2.3.4"),
    ("Antwort von 1.2.3.4: x=5ms", "1.2.3.4"),
    ("Reply 1.2.3.4: :1.2.3.4: Zeit=7ms", "1.2.3.4"),
    ("::1.2.3.4: Zeit=7ms", "1.2.3.4"),
    ("::ffff:1.2.3.4: Zeit=7ms", "1.2.3.4"),
    ("::ffff:1.2.3.4: Zeit=7ms", "::ffff:1.2.3.4"),
    ("a.1.2.3.4: Zeit=7ms", "1.2.3.4"),
    ("%1.2.3.4: Zeit=7ms", "1.2.3.4"),
    ("1.2.3.4 \u3000: Zeit=7ms", "1.2.3.4"),
    ("1.2.3.4:\u2028Zeit=7ms", "1.2.3.4"),
    ("1.2.3.4::\tZeit=7ms", "1.2.3.4"),
    ("01.2.3.4: Zeit=7ms", "1.2.3.4"),
    ("1.2.3.4: 1.2.3.5: Zeit=7ms", "1.2.3.5"),
    ("TIME=2MS", None),
    ("time = 3 ms", None),
    ("time\x1c=\x1c4\x1cms", None),
    ("time\u00a0<\u20055\u3000ms", None),
    ("time\n=\n6\nms", None),
    ("t\u0130me=5ms", None),
    ("t\u0131me=5ms", None),
    ("\u017fome time=6 m\u017f", None),
    ("ti\u1e3fe=5ms", None),
    ("time=6msx", None),
    ("time=1.ms", None),
    ("time=.5ms", None),
    ("time=1.5.6ms", None),
    ("time=1,5,6 ms", None),
    ("time==2ms time=3ms", None),
    ("ttl=64 Zeit<1ms", None),
    ("ttl=64 Zeit<1msx", None),
    ("ttl=64 Zeit<1ms\u0301", None),
    ("ttl=64 Zeit<1ms\u00b2", None),
    ("ttl=64 Zeit<1ms\u0663", None),
    ("ttl=64 Zeit<1ms\u2160", None),
    ("ttl=64 Zeit<1ms\u00aa", None),
    ("ttl=64 Zeit<1ms\u0345", None),
    ("ttl=64 Zeit<1ms_", None),
    ("ttl=64 Zeit<1ms-", None),
    ("ttl=64 Zeit=1ms<2ms ", None),
    ("ttl=64 Zeit=1msx Zeit=2ms", None),
    ("ttl=64 Zeit=1.5msx Zeit=2ms", None),
    ("ttl=64 Zeit=1.5 m\u017f", None),
    ("xttl=64 Zeit<1ms", None),
    ("_ttl=64 Zeit<1ms", None),
    ("-ttl=64 Zeit<1ms", None),
    ("\u0301ttl=64 Zeit<1ms", None),
    ("\u00e9ttl=64 Zeit<1ms", None),
    ("\u2160ttl=64 Zeit<1ms", None),
    ("TTL = 64 Zeit = 9,5 MS", None),
    ("T\u0130L=1 Zeit=2ms", None),
    ("T\u0131L=1 Zeit=2ms", None),
    ("ttl=x Zeit=2ms", None),
    ("ttl= Zeit=2ms", None),
    ("ttl=64\nZeit=2ms", None),
    ("ttl=64\x0bZeit=2ms", None),
    ("ttl=64\x1dZeit=2ms", None),
    ("ttl=64\x85Zeit=2ms", None),
    ("ttl=64\r\nZeit=2ms", None),
    ("ttl=64 \u2028 Zeit=2ms", None),
    ("ttl=64 \u2029 Zeit=2ms", None),
    ("ttl=64 Zeit=\u0662ms", None),
    ("ttl=64 Zeit=1.ms", None),
    ("ttl=64 Zeit=1.5.6ms", None),
    ("ttl=64 Zeit=1.5.6 ms", None),
    ("", None),
    ("", "1.2.3.4"),
]

#: (returncode, stdout, stderr) for ``_error_detail``.
ERROR_DETAIL_CORPUS: list[tuple[int, str, str]] = [
    (1, LINUX_NO_REPLY, ""),
    (1, LINUX_UNREACHABLE, ""),
    (2, "", LINUX_NO_ROUTE),
    (2, "", LINUX_DENIED),
    (2, MACOS_NO_REPLY, ""),
    (2, "", "ping: socket: Operation not permitted\n"),
    (2, "", "first\n  last line  \n\n"),
    (2, "out unreachable\n", "plain stderr\n"),
    (2, "x" * 300 + " error\n", ""),
    (2, "", "\u00e9" * 250 + " failed"),
    (2, "", "\U0001f600" * 199 + "\u00e9\u00e9 failed"),
    (1, "Network is UNREACHABLE\n", "UNKNOWN host\n"),
    (1, "", ""),
    (1, "a\rb error\r\n", ""),
    (1, "a\x1cb Denied", ""),
    (1, "a\x1db\x1ec\x85d\x0be\x0cf cannot", ""),
    (1, "\u0130 cannot", ""),
    (1, "CANNOT\u0130", ""),
    (1, "", "   \n\t\n"),
    (1, "", "\u3000x\u3000\n"),
    (1, "line\u2028second exceeded", ""),
    (1, "time to live EXCEEDED", "warning only"),
    (1, "No Route to host", ""),
    (1, "noroute", ""),
    (1, "Request timeout for icmp_seq 0\n", ""),
]

PERMISSION_CORPUS = [
    "ping: socket: Operation not permitted",
    LINUX_DENIED,
    "PERMISSION DENIED",
    "ping: You must be root to use this",
    "must run as root",
    "requires root privileges",
    "Access denied.",
    "nothing to see",
    "",
    "OPERATION NOT\u00a0PERMITTED",
    "\u0130 operation not permitted",
    "operation not\npermitted",
    "MUST BE R\u00d6OT",
    "ACCESS DEN\u0130ED",
]

PING_PLATFORMS = [
    "linux",
    "linux2",
    "darwin",
    "freebsd14",
    "openbsd7",
    "netbsd10",
    "win32",
    "cygwin",
    "sunos5",
]
PING_ADDRESSES = [
    "192.0.2.1",
    "2001:db8::1",
    "fe80::1%en0",
    "::ffff:192.0.2.1",
    "192.0.2.1%x",
    "fe80::1%",
]
PING_TIMEOUTS = [
    0.2,
    1.0,
    1.5,
    2.5,
    0.0001,
    60.0,
    0.1 + 0.2,
    1.001,
    0.0005,
    0.0015,
    2.0000000001,
    5e-324,
    59.9999999999,
]

#: ``bytes.decode(errors="replace")`` of process output.
DECODE_CORPUS: list[bytes] = [
    b"",
    b"time=1.5 ms\n",
    b"\xff",
    b"Zeit\xe9",
    b"a\xed\xa0\x80b",
    b"\xf0\x9f\x98",
    b"\xf0\x9f\x98\x80",
    b"\xc3",
    b"\xc3(",
    b"\xe2\x82",
    b"\xe2\x82\xac",
    b"\xf4\x90\x80\x80",
    b"\xf5\x80\x80\x80",
    b"\xc0\xaf",
    b"\xe0\x80\xaf",
    b"\xf8\x88\x80\x80\x80",
    b"\x80\x80",
    b"a\x00b",
]


def _packet_stats_cases() -> list[dict[str, Any]]:
    from nyxmon.adapters.runner.executors.ping_executor import PingCheckExecutor

    return [
        {
            "sent": sent,
            "received": received,
            "stats_json": json.dumps(PingCheckExecutor._packet_stats(sent, received)),
        }
        for sent in range(0, 21)
        for received in range(0, sent + 1)
    ]


def _rtt_average_cases() -> list[dict[str, Any]]:
    rng = random.Random(6021)
    lists: list[list[float]] = [
        [1.5, 0.5, 1.0],
        [0.1, 0.2, 0.3],
        [1e16, 1.0, -1e16],
        [1e16, 1.0],
        [0.1] * 20,
        [3.512],
        [0.042, 12.0, 0.333],
        [0.0, 0.0],
        [float("inf"), 1.0],
        [1e308, 1e308],
        [0.0005, 0.0015, 0.0025],
        [x / 7 for x in range(1, 21)],
    ]
    for _ in range(80):
        size = rng.randint(1, 20)
        lists.append(
            [round(rng.uniform(0, 300), rng.randint(0, 3)) for _ in range(size)]
        )
    return [
        {
            "rtts": rtts,
            "min": min(rtts),
            "max": max(rtts),
            "avg": round(sum(rtts) / len(rtts), 3),
        }
        for rtts in lists
    ]


# ------------------------------------------------------------ executor runs


class _PingStubRunner:
    def __init__(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[Any]] = []

    async def __call__(self, command: Any, timeout: float) -> Any:
        from nyxmon.adapters.runner.executors.ping_executor import ProcessOutput

        self.calls.append([list(command), timeout])
        outcome = self.outcomes.pop(0)
        kind = outcome.get("raise")
        if kind == "timeout":
            raise TimeoutError()
        if kind == "not_found":
            raise FileNotFoundError(2, "No such file or directory", command[0])
        if kind == "permission":
            raise PermissionError(13, "Permission denied", command[0])
        if kind == "exec_format":
            raise OSError(8, "Exec format error", command[0])
        return ProcessOutput(
            outcome["returncode"], outcome["stdout"], outcome["stderr"]
        )


def _reply(rtt: float) -> dict[str, Any]:
    return {"returncode": 0, "stdout": LINUX_REPLY.format(rtt=rtt), "stderr": ""}


_NO_REPLY = {"returncode": 1, "stdout": LINUX_NO_REPLY, "stderr": ""}
_UNREACHABLE = {"returncode": 1, "stdout": LINUX_UNREACHABLE, "stderr": ""}
_NO_ROUTE = {"returncode": 2, "stdout": "", "stderr": LINUX_NO_ROUTE}
_DENIED = {"returncode": 2, "stdout": "", "stderr": LINUX_DENIED}

#: Executor runs: (name, data JSON, url, platform, resolver, process outcomes).
#: ``resolver`` is a list of addresses, ``{"error": message}`` or ``"hang"``.
PING_SCENARIOS: list[tuple[str, str, str, str, Any, list[dict[str, Any]]]] = [
    ("all_ok", "{}", "192.0.2.1", "linux", [], [_reply(1.5), _reply(0.5), _reply(1.0)]),
    (
        "partial_loss",
        "{}",
        "192.0.2.1",
        "linux",
        [],
        [_NO_REPLY, _reply(2.0), {"raise": "timeout"}],
    ),
    (
        "interval_and_timeout",
        '{"count": 2, "interval": 0.25, "timeout": 1.5}',
        "192.0.2.1",
        "linux",
        [],
        [_reply(1.0), _reply(1.0)],
    ),
    (
        "zero_interval",
        '{"count": 3, "interval": 0}',
        "192.0.2.1",
        "linux",
        [],
        [_reply(1.0), _NO_REPLY, _reply(0.1)],
    ),
    (
        "negative_zero_interval",
        '{"count": 2, "interval": -0.0}',
        "192.0.2.1",
        "linux",
        [],
        [_reply(1.0), _reply(2.0)],
    ),
    (
        "all_timeouts",
        "{}",
        "192.0.2.1",
        "linux",
        [],
        [{"raise": "timeout"}, _NO_REPLY, {"raise": "timeout"}],
    ),
    (
        "all_timeouts_fractional",
        '{"count": 2, "timeout": 0.25}',
        "192.0.2.1",
        "linux",
        [],
        [_NO_REPLY, _NO_REPLY],
    ),
    ("unreachable", "{}", "192.0.2.1", "linux", [], [_UNREACHABLE] * 3),
    ("no_route", '{"count": 1}', "192.0.2.1", "linux", [], [_NO_ROUTE]),
    (
        "mixed_failures",
        "{}",
        "192.0.2.1",
        "linux",
        [],
        [_UNREACHABLE, {"raise": "timeout"}, _NO_REPLY],
    ),
    (
        "timeout_then_unreachable",
        '{"timeout": 2.5}',
        "192.0.2.1",
        "linux",
        [],
        [{"raise": "timeout"}, _UNREACHABLE, {"raise": "timeout"}],
    ),
    (
        "permission_output",
        "{}",
        "192.0.2.1",
        "linux",
        [],
        [_DENIED, _reply(1.0), _reply(1.0)],
    ),
    (
        "permission_after_ok",
        "{}",
        "192.0.2.1",
        "linux",
        [],
        [_reply(1.0), _DENIED, _reply(1.0)],
    ),
    (
        "permission_without_detail",
        '{"count": 1}',
        "192.0.2.1",
        "linux",
        [],
        [{"returncode": 2, "stdout": "MUST BE ROOT", "stderr": ""}],
    ),
    ("permission_spawn", "{}", "192.0.2.1", "linux", [], [{"raise": "permission"}]),
    ("missing_binary", "{}", "192.0.2.1", "linux", [], [{"raise": "not_found"}]),
    (
        "exec_format",
        '{"count": 2}',
        "192.0.2.1",
        "linux",
        [],
        [{"raise": "exec_format"}, _reply(3.0)],
    ),
    (
        "exec_format_only",
        '{"count": 2}',
        "192.0.2.1",
        "linux",
        [],
        [{"raise": "exec_format"}, {"raise": "timeout"}],
    ),
    (
        "exit_zero_without_rtt",
        '{"count": 1}',
        "192.0.2.1",
        "linux",
        [],
        [{"returncode": 0, "stdout": "garbage\n", "stderr": ""}],
    ),
    (
        "rtt_with_failure_code",
        '{"count": 1}',
        "192.0.2.1",
        "linux",
        [],
        [{"returncode": 1, "stdout": LINUX_REPLY.format(rtt=1), "stderr": ""}],
    ),
    (
        "rtt_in_stderr_only",
        '{"count": 1}',
        "192.0.2.1",
        "linux",
        [],
        [{"returncode": 0, "stdout": "", "stderr": "time=1 ms"}],
    ),
    (
        "signal_killed",
        '{"count": 1}',
        "192.0.2.1",
        "linux",
        [],
        [{"returncode": -9, "stdout": "", "stderr": ""}],
    ),
    (
        "huge_rtt",
        '{"count": 2}',
        "192.0.2.1",
        "linux",
        [],
        [
            {"returncode": 0, "stdout": "time=" + "9" * 400 + " ms", "stderr": ""},
            _reply(1.0),
        ],
    ),
    (
        "windows_router",
        '{"count": 1}',
        "192.0.2.1",
        "win32",
        [],
        [{"returncode": 0, "stdout": WINDOWS_ROUTER_UNREACHABLE, "stderr": ""}],
    ),
    (
        "windows_localized",
        '{"count": 2}',
        "192.0.2.1",
        "win32",
        [],
        [
            {"returncode": 0, "stdout": WINDOWS_REPLY_DE, "stderr": ""},
            {"returncode": 0, "stdout": WINDOWS_UNREACHABLE_DE, "stderr": ""},
        ],
    ),
    (
        "windows_v6",
        '{"count": 2}',
        "2001:db8::1",
        "win32",
        [],
        [
            {"returncode": 0, "stdout": WINDOWS_UNREACHABLE_V6_DE, "stderr": ""},
            {"returncode": 0, "stdout": WINDOWS_REPLY_V6_DE, "stderr": ""},
        ],
    ),
    (
        "linux_v6",
        '{"count": 1}',
        "::1",
        "linux",
        [],
        [{"returncode": 0, "stdout": LINUX_REPLY_V6, "stderr": ""}],
    ),
    (
        "darwin_v4",
        '{"count": 1, "timeout": 2.5}',
        "192.0.2.1",
        "darwin",
        [],
        [{"returncode": 0, "stdout": MACOS_REPLY, "stderr": ""}],
    ),
    (
        "darwin_no_reply",
        '{"count": 1, "timeout": 1}',
        "192.0.2.1",
        "darwin",
        [],
        [{"returncode": 2, "stdout": MACOS_NO_REPLY, "stderr": ""}],
    ),
    (
        "darwin_v6",
        '{"count": 1}',
        "fe80::1%en0",
        "darwin",
        [],
        [{"returncode": 0, "stdout": MACOS_PING6_REPLY, "stderr": ""}],
    ),
    (
        "resolved",
        '{"count": 1}',
        "gateway.example.test",
        "linux",
        ["192.0.2.10", "2001:db8::10"],
        [_reply(1.0)],
    ),
    (
        "resolved_v6_first",
        '{"count": 1}',
        "gateway.example.test",
        "darwin",
        ["2001:db8::10", "192.0.2.10"],
        [_reply(1.0)],
    ),
    (
        "resolved_same_as_host",
        '{"count": 1}',
        "gateway.example.test",
        "linux",
        ["gateway.example.test"],
        [_reply(1.0)],
    ),
    (
        "dns_error",
        "{}",
        "nas.invalid",
        "linux",
        {"error": "[Errno -2] Name or service not known"},
        [],
    ),
    ("dns_empty", "{}", "nas.example.test", "linux", [], []),
    ("dns_timeout", '{"timeout": 0.05}', "slow.example.test", "linux", "hang", []),
    ("config_error", '{"count": 0}', "192.0.2.1", "linux", [], []),
    ("no_target", "{}", "  ", "linux", [], []),
    (
        "url_target",
        '{"count": 1}',
        "https://router.example.test:8443/status",
        "linux",
        ["192.0.2.99"],
        [_reply(1.0)],
    ),
    (
        "host_override",
        '{"count": 1, "host": "192.0.2.5"}',
        "ignored.example.test",
        "linux",
        [],
        [_reply(1.0)],
    ),
    ("ipv4_with_zone", '{"count": 1}', "192.0.2.1%x", "linux", [], [_reply(1.0)]),
    (
        "many_rtts",
        '{"count": 20, "interval": 0.5, "timeout": 0.3}',
        "192.0.2.1",
        "freebsd14",
        [],
        [_reply(x / 7) for x in range(1, 21)],
    ),
    (
        "other_bsd",
        '{"count": 1, "timeout": 2}',
        "192.0.2.1",
        "openbsd7",
        [],
        [_reply(1.0)],
    ),
]


def _ping_scenario(
    name: str,
    data_json: str,
    url: str,
    platform: str,
    resolver: Any,
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    from nyxmon.adapters.runner.executors.ping_executor import PingCheckExecutor
    from nyxmon.domain import Check, CheckType

    import anyio

    runner = _PingStubRunner(outcomes)
    resolver_calls: list[str] = []
    sleeps: list[float] = []

    async def resolve(host: str) -> list[str]:
        resolver_calls.append(host)
        if resolver == "hang":
            await anyio.sleep(10)
        if isinstance(resolver, dict):
            raise OSError(resolver["error"])
        return list(resolver)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    executor = PingCheckExecutor(
        run=runner,
        resolver=resolve,
        sleep=sleep,
        platform=platform,
        ping_binary="/bin/ping",
        ping6_binary="/sbin/ping6",
    )
    check = Check(
        check_id=7,
        service_id=1,
        name="ping",
        check_type=CheckType.PING,
        url=url,
        data=json.loads(data_json),
    )
    case: dict[str, Any] = {
        "name": name,
        "data_json": data_json,
        "url": url,
        "platform": platform,
        "resolver": resolver,
        "outcomes": outcomes,
    }
    try:
        result = _run_async(lambda: executor.execute(check))
    except Exception as exc:  # noqa: BLE001 - the runner's executor_exception
        case["exception"] = type(exc).__name__
    else:
        case["status"] = str(result.status)
        case["result_json"] = json.dumps(result.data)
    case["runner_calls"] = runner.calls
    case["resolver_calls"] = resolver_calls
    case["sleeps"] = sleeps
    return case


def build_ping_text() -> dict[str, Any]:
    """Ping command lines, output parsers, statistics and scripted runs."""
    from nyxmon.adapters.runner.executors import ping_executor as ping

    commands = []
    for platform in PING_PLATFORMS:
        for address in PING_ADDRESSES:
            for timeout in PING_TIMEOUTS:
                for ping6 in (None, "/sbin/ping6"):
                    commands.append(
                        {
                            "platform": platform,
                            "address": address,
                            "timeout": timeout,
                            "ping6": ping6,
                            "command": ping.build_ping_command(
                                platform, address, timeout, "/bin/ping", ping6
                            ),
                        }
                    )
    return {
        "constants": {
            "process_grace_seconds": ping.PROCESS_GRACE_SECONDS,
            "fallback_ping_paths": list(ping.FALLBACK_PING_PATHS),
            "fallback_ping6_paths": list(ping.FALLBACK_PING6_PATHS),
            "max_error_detail": ping.MAX_ERROR_DETAIL,
            "permission_hint": ping.PERMISSION_HINT,
        },
        "commands": commands,
        "rtt": [
            {
                "output": output,
                "address": address,
                "rtt": ping.parse_rtt_ms(output, address),
            }
            for output, address in RTT_CORPUS
        ],
        "error_detail": [
            {
                "returncode": rc,
                "stdout": stdout,
                "stderr": stderr,
                "detail": ping._error_detail(ping.ProcessOutput(rc, stdout, stderr)),
            }
            for rc, stdout, stderr in ERROR_DETAIL_CORPUS
        ],
        "permission": [
            {"output": text, "permission": ping.is_permission_failure(text)}
            for text in PERMISSION_CORPUS
        ],
        "decode": [
            {"hex": data.hex(), "text": data.decode(errors="replace")}
            for data in DECODE_CORPUS
        ],
        "packet_stats": _packet_stats_cases(),
        "rtt_stats": _rtt_average_cases(),
        "scenarios": [_ping_scenario(*scenario) for scenario in PING_SCENARIOS],
    }


# ------------------------------------------------------- character classes


def _match_ranges(pattern: str, flags: int = 0) -> list[list[int]]:
    """``[start, end]`` ranges of the code points ``pattern`` matches alone."""
    every = "".join(map(chr, range(0x110000)))
    ranges: list[list[int]] = []
    for match in re.finditer(pattern, every, flags):
        code = match.start()
        if ranges and ranges[-1][1] == code - 1:
            ranges[-1][1] = code
        else:
            ranges.append([code, code])
    return ranges


def build_ping_unicode() -> dict[str, Any]:
    """The ``re``/``str`` character classes behind the ping parsers."""
    import unicodedata

    every = "".join(map(chr, range(0x110000)))
    separators = [
        code
        for code in range(0x110000)
        if len(("a" + chr(code) + "b").splitlines()) == 2
    ]
    return {
        "unidata_version": unicodedata.unidata_version,
        "word": _match_ranges(r"\w"),
        "space": _match_ranges(r"\s"),
        "ignorecase": {
            letter: [m.start() for m in re.finditer(letter, every, re.IGNORECASE)]
            for letter in "eilmst"
        },
        "line_separators": separators,
    }


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "config_ping.json": build_config_ping,
    "ping_text.json": build_ping_text,
    "ping_unicode.json": build_ping_unicode,
}
