"""Fixture builders of slice S5b (DNS check), loaded by ``generate.py``.

* ``config_dns.json``: ``DnsCheckConfig.from_dict`` + ``validate`` outcomes
  for a data corpus (``cases``) and ``executor_cases``: the real
  ``DnsCheckExecutor`` (dnspython) run against a scripted local DNS server
  on ``127.0.0.1`` (UDP and TCP on one port). Each case records the wire
  responses (templates whose ID the server replaces by the query's ID), the
  queries the server received, and the result with its key order.
* ``dns_text.json``: ``normalize_dns_value``, ``expected_ip_error``,
  ``_check_ip_match``, ``dns.name.from_text`` + ``to_text`` (the query
  name), dnspython's IPv6 text form and ``bytes.decode()`` errors of TXT
  strings.
"""

from __future__ import annotations

import asyncio
import errno
import io
import struct
from collections.abc import Callable, Mapping
from typing import Any
from unittest import mock


def _normalise_set_reprs(message: str) -> str:
    from nyxmon.domain import dns_config

    constant = dns_config.VALID_QUERY_TYPES
    rendered = "{" + ", ".join(repr(item) for item in sorted(constant)) + "}"
    return message.replace(repr(constant), rendered)


# ------------------------------------------------------------- config cases

_NUMBERS: list[Any] = [
    0,
    0.0,
    -1,
    0.001,
    1,
    5.0,
    300,
    300.0,
    300.5,
    301,
    True,
    False,
    "5",
    None,
    [],
    {},
    2**63,
    10**400,
    float("inf"),
    float("nan"),
]

_ADDRESS_VALUES: list[Any] = [
    "192.0.2.1",
    "2001:db8::1",
    "fe80::1%lo0",
    "::ffff:192.0.2.1",
    " 192.0.2.1",
    "192.0.2.1 ",
    "01.2.3.4",
    "1.2.3",
    "eth0",
    "dns.example",
    "",
    None,
    0,
    5,
    -1,
    2**32,
    2**128,
    True,
    False,
    1.5,
    0.0,
    [],
    ["192.0.2.1"],
    {},
    {"a": 1},
]


def _config_corpus() -> list[Any]:
    base = {"expected_ips": ["192.0.2.1"]}
    corpus: list[Any] = [
        {},
        [],
        ["expected_ips"],
        "x",
        "expected_ips",
        5,
        1.5,
        True,
        False,
        base,
        {"expected_ips": []},
        {"expected_ips": "192.0.2.1"},
        {"expected_ips": None},
        {"expected_ips": [1]},
        {"expected_ips": ["192.0.2.1", None]},
        {"expected_ips": {"192.0.2.1": 1}},
        {"expected_ips": ["2001:db8::1"]},
        {"expected_ips": ["2001:db8::1"], "query_type": "AAAA"},
        {"expected_ips": ["192.0.2.1"], "query_type": "AAAA"},
        {"expected_ips": ["::ffff:192.0.2.1"], "query_type": "A"},
        {"expected_ips": ["::ffff:192.0.2.1"], "query_type": "AAAA"},
        {"expected_ips": ["2A01:04F8:0000::0001"], "query_type": "AAAA"},
        {"expected_ips": [" 192.0.2.1 "]},
        {"expected_ips": ["192.0.2.1\n"]},
        {"expected_ips": ["mail.example.com."], "query_type": "MX"},
        {"expected_ips": ["mail.example.com."], "query_type": "A"},
        {"expected_ips": ["fe80::1%eth0"], "query_type": "AAAA"},
        {"expected_ips": ["v=spf1 -all"], "query_type": "TXT"},
        {"expected_ips": ["x"], "query_type": "CNAME"},
        {"expected_ips": ["x"], "query_type": "NS"},
        {"expected_ips": ["x"], "query_type": "SOA"},
        {"expected_ips": ["x"], "query_type": "PTR"},
        {"expected_ips": ["x"], "query_type": "ANY"},
        {"expected_ips": ["x"], "query_type": "a"},
        {"expected_ips": ["x"], "query_type": ""},
        {"expected_ips": ["x"], "query_type": None},
        {"expected_ips": ["x"], "query_type": 5},
        {"expected_ips": ["x"], "query_type": True},
        {"expected_ips": ["x"], "query_type": 1.5},
        {"expected_ips": ["x"], "query_type": ["A"]},
        {"expected_ips": ["x"], "query_type": {"A": 1}},
        # Error precedence: query_type, timeout, expected_ips, source, server.
        {"expected_ips": ["x"], "query_type": "BAD", "timeout": 0},
        {"expected_ips": ["x"], "timeout": 0},
        {"expected_ips": ["x"], "source_ip": "bad", "dns_server": "bad"},
        {**base, "source_ip": "bad", "dns_server": "bad", "timeout": 0},
        {**base, "source_ip": "bad", "dns_server": "bad"},
        {**base, "dns_server": "bad"},
        {**base, "extra": 1},
    ]
    corpus.extend({**base, "timeout": value} for value in _NUMBERS)
    for name in ("dns_server", "source_ip"):
        corpus.extend({**base, name: value} for value in _ADDRESS_VALUES)
    return corpus


def _exception(exc: BaseException) -> dict[str, Any]:
    return {"class": type(exc).__name__, "message": str(exc)}


def _config_outcome(data: Any) -> dict[str, Any]:
    from nyxmon.domain.dns_config import DnsCheckConfig

    try:
        config = DnsCheckConfig.from_dict(data)
        config.validate()
    except ValueError as exc:
        return {"error": _normalise_set_reprs(str(exc))}
    except Exception as exc:  # noqa: BLE001 - the executor reports str(exc)
        return {"exception": _exception(exc)}
    return {
        "ok": {
            "expected_ips": config.expected_ips,
            "dns_server": config.dns_server,
            "source_ip": config.source_ip,
            "query_type": config.query_type,
            "timeout": config.timeout,
        }
    }


# ----------------------------------------------------------- text helpers


def _normalize_corpus() -> list[str]:
    return [
        "192.0.2.1",
        " 192.0.2.1 ",
        "192.0.2.1\t",
        "\u00a0192.0.2.1",
        "192.000.2.1",
        "01.2.3.4",
        "2001:DB8::1",
        "2A01:04F8:0000::0001",
        "2001:db8:0:0:1:0:0:1",
        "2001:db8::1:0:0:1",
        "1:0:1:0:1:0:1:0",
        "0:0:0:0:0:0:0:0",
        "::",
        "::1",
        "1::",
        "::ffff:192.0.2.1",
        "::FFFF:C000:0201",
        "::192.0.2.1",
        "::ffff:0:0",
        "64:ff9b::192.0.2.1",
        "fe80::1%eth0",
        "FE80::1%Eth0",
        "fe80::1%",
        "1:2:3:4:5:6:7::",
        "::2:3:4:5:6:7:8",
        "1:2:3:4:5:6:7:8:9",
        "12345::",
        "mail.example.com.",
        "v=spf1 -all",
        "",
        " ",
        "[::1]",
        "1.2.3.4/32",
    ]


def _ip_match_cases() -> list[tuple[list[str], list[str], bool]]:
    return [
        (["192.0.2.1"], ["192.0.2.1"], True),
        (["192.0.2.1"], ["192.0.2.2"], True),
        (["192.0.2.1", "192.0.2.2"], ["192.0.2.2"], True),
        (["2a01:4f8::1"], ["2A01:04F8:0000::0001"], True),
        (["2a01:4f8::1"], ["2a01:4f8::2"], True),
        (["::ffff:192.0.2.1"], ["::ffff:c000:201"], True),
        (["mail.example.com."], ["mail.example.com."], True),
        (["mail.example.com."], ["MAIL.example.com."], True),
        (["192.0.2.1"], [" 192.0.2.1"], True),
        (["192.0.2.1"], [" 192.0.2.1"], False),
        (["2a01:4f8::1"], ["2A01:04F8::1"], False),
        (["v=spf1 -all"], ["v=spf1 -all"], False),
        ([], ["192.0.2.1"], True),
        (["fe80::1%eth0"], ["FE80::1%eth0"], True),
        (["fe80::1%eth0"], ["fe80::1%Eth0"], True),
    ]


def _name_corpus() -> list[str]:
    return [
        "example.com",
        "example.com.",
        "Example.COM",
        "home.xn--wersdrfer-47a.de",
        "a",
        "",
        ".",
        "@",
        "a.@",
        "a..b",
        ".a",
        "a b",
        "a\tb",
        'a"b',
        "a(b).c;d@e$f",
        "x\\.y",
        "x\\065y",
        "x\\255y",
        "x\\000y",
        "x\\999y",
        "x\\",
        "x\\1",
        "x\\12y",
        "x\\\\y",
        "_dmarc.example.com",
        "*.example.com",
        "a" * 63 + ".com",
        "a" * 64 + ".com",
        ".".join(["a" * 63] * 3 + ["a" * 61]),
        ".".join(["a" * 63] * 3 + ["a" * 62]),
        ".".join(["a" * 63] * 3 + ["a" * 61]) + ".",
        "https://example.com/",
        "1.2.3.4",
    ]


def _name_outcome(text: str) -> dict[str, Any]:
    import dns.name

    try:
        name = dns.name.from_text(text, None)
        if not name.is_absolute():
            name = name.concatenate(dns.name.root)
    except Exception as exc:  # noqa: BLE001 - the executor reports str(exc)
        return {"exception": _exception(exc)}
    return {"text": name.to_text(), "wire_hex": name.to_wire().hex()}


def _ipv6_corpus() -> list[str]:
    return [
        "::",
        "::1",
        "1::",
        "::0.0.0.1",
        "::1.2.3.4",
        "::ffff:1.2.3.4",
        "::ffff:0:0",
        "::fffe:1.2.3.4",
        "0:0:0:0:0:1:0:0",
        "1:0:0:1:0:0:0:1",
        "1:0:0:0:1:0:0:1",
        "1:0:1:0:1:0:1:0",
        "2001:db8::1",
        "2001:db8:0:0:1::1",
        "fe80::abcd:ef01",
        "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "0:0:0:0:0:ffff:0:1",
        "1:2:3:4:5:6:7:0",
        "0:1:2:3:4:5:6:7",
    ]


def _decode_corpus() -> list[bytes]:
    return [
        b"",
        b"plain",
        "caf\u00e9".encode(),
        "\U0001f600".encode(),
        b"\xff",
        b"ab\x80",
        b"ab\xc3",
        b"ab\xc3(",
        b"\xc0\xaf",
        b"\xc1\xbf",
        b"\xe0\x80\x80",
        b"\xe0\xa0",
        b"\xe0\xa0x",
        b"\xed\xa0\x80",
        b"\xf0\x90\x80",
        b"\xf0\x90\x80x",
        b"\xf4\x90\x80\x80",
        b"\xf5\x80\x80\x80",
        b"\xf8",
        b"ok\xfe\xff",
    ]


def build_dns_text() -> dict[str, Any]:
    import dns.ipv6
    from nyxmon.adapters.runner.executors.dns_executor import DnsCheckExecutor
    from nyxmon.domain.dns_config import expected_ip_error, normalize_dns_value

    executor = DnsCheckExecutor(resolver=mock.Mock())
    decode_cases = []
    for raw in _decode_corpus():
        try:
            outcome: dict[str, Any] = {"text": raw.decode()}
        except UnicodeDecodeError as exc:
            outcome = {"error": str(exc)}
        decode_cases.append({"hex": raw.hex(), **outcome})
    return {
        "normalize": [
            {"value": value, "result": normalize_dns_value(value)}
            for value in _normalize_corpus()
        ],
        "expected_ip_error": [
            {
                "value": value,
                "query_type": query_type,
                "result": expected_ip_error(value, query_type),
            }
            for value in _normalize_corpus()
            for query_type in ("A", "AAAA", "MX", "TXT")
        ],
        "ip_match": [
            {
                "resolved": resolved,
                "expected": expected,
                "compare_addresses": compare,
                "result": executor._check_ip_match(
                    resolved, expected, compare_addresses=compare
                ),
            }
            for resolved, expected, compare in _ip_match_cases()
        ],
        "names": [{"input": text, **_name_outcome(text)} for text in _name_corpus()],
        "ipv6": [
            {
                "hex": dns.ipv6.inet_aton(text).hex(),
                "text": dns.ipv6.inet_ntoa(dns.ipv6.inet_aton(text)),
            }
            for text in _ipv6_corpus()
        ],
        "utf8_decode": decode_cases,
    }


# ----------------------------------------------------------- executor cases


def _response(
    qname: str,
    qtype: str,
    *,
    rcode: str = "NOERROR",
    answer: list[tuple[str, str, str]] | None = None,
    authority: list[tuple[str, str, str]] | None = None,
    tc: bool = False,
    question: tuple[str, str] | None = None,
    no_question: bool = False,
    qr: bool = True,
    edns: bool = False,
) -> str:
    """A response template (ID 0) built with dnspython."""
    import dns.flags
    import dns.message
    import dns.rcode
    import dns.rrset

    qn, qt = question or (qname, qtype)
    query = dns.message.make_query(qn, qt)
    query.id = 0
    response = dns.message.make_response(query)
    response.set_rcode(dns.rcode.from_text(rcode))
    if tc:
        response.flags |= dns.flags.TC
    if not qr:
        response.flags &= ~dns.flags.QR
    if no_question:
        response.question = []
    # One RRset per record, so duplicates reach the wire as sent.
    for section, records in (
        (response.answer, answer),
        (response.authority, authority),
    ):
        for owner, rtype, text in records or []:
            section.append(dns.rrset.from_text(owner, 300, "IN", rtype, text))
    if edns:
        response.use_edns(0, 0, 1232)
    return response.to_wire().hex()


def _reply(*templates: str, id_mode: str = "match") -> dict[str, Any]:
    return {"reply": list(templates), "id": id_mode}


_DROP: dict[str, Any] = {"drop": True}
_CLOSE: dict[str, Any] = {"close": True}

_SERVER = "127.0.0.1"
_DEFAULT_RESOLV = "nameserver 127.0.0.1\n"


class _ScriptedDns:
    """UDP and TCP DNS server on one loopback port playing scripted steps."""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self.port = 0

    def _record(self, wire: bytes, transport: str) -> int:
        import dns.message
        import dns.rdatatype

        query = dns.message.from_wire(wire)
        question = query.question[0]
        self.requests.append(
            {
                "transport": transport,
                "qname": question.name.to_text(),
                "qtype": dns.rdatatype.to_text(question.rdtype),
                "flags": int(query.flags),
                "edns": query.edns,
                "payload": query.payload if query.edns >= 0 else 0,
            }
        )
        return query.id

    def _answers(self, query_id: int) -> list[bytes] | None:
        step = self.steps.pop(0) if self.steps else _DROP
        if "reply" not in step:
            return None if "drop" in step else []
        answers = []
        for template in step["reply"]:
            wire = bytearray(bytes.fromhex(template))
            if len(wire) >= 2:
                reply_id = query_id if step["id"] == "match" else query_id ^ 0xFFFF
                wire[0:2] = struct.pack("!H", reply_id)
            answers.append(bytes(wire))
        return answers

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        server = self

        class Udp(asyncio.DatagramProtocol):
            def connection_made(self, transport: Any) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr: Any) -> None:
                answers = server._answers(server._record(data, "udp"))
                for answer in answers or []:
                    self.transport.sendto(answer, addr)

        async def handle(reader: Any, writer: Any) -> None:
            try:
                length = struct.unpack("!H", await reader.readexactly(2))[0]
                data = await reader.readexactly(length)
            except Exception:  # noqa: BLE001 - client went away
                writer.close()
                return
            answers = self._answers(self._record(data, "tcp"))
            if answers is None:
                await asyncio.sleep(5)
            for answer in answers or []:
                writer.write(struct.pack("!H", len(answer)) + answer)
            await writer.drain()
            writer.close()

        # The TCP listener needs the UDP port; another socket (e.g. a client
        # connection of an earlier fixture) may already hold it over TCP.
        for _ in range(50):
            self.udp, _ = await loop.create_datagram_endpoint(
                Udp, local_addr=(_SERVER, 0)
            )
            self.port = self.udp.get_extra_info("sockname")[1]
            try:
                self.tcp = await asyncio.start_server(handle, _SERVER, self.port)
            except OSError as exc:
                self.udp.close()
                if exc.errno != errno.EADDRINUSE:
                    raise
                continue
            return
        raise RuntimeError("no port free for both UDP and TCP")

    def close(self) -> None:
        self.udp.close()
        self.tcp.close()


def _run_executor_case(
    url: str,
    data: Any,
    steps: list[dict[str, Any]],
    resolv_conf: str = _DEFAULT_RESOLV,
) -> dict[str, Any]:
    import dns.resolver

    from nyxmon.adapters.runner.executors.dns_executor import DnsCheckExecutor
    from nyxmon.domain import Check

    server = _ScriptedDns(steps)
    original_reset = dns.resolver.BaseResolver.reset
    original_read = dns.resolver.BaseResolver.read_resolv_conf

    def reset(self: Any) -> None:
        original_reset(self)
        self.port = server.port

    def read_resolv_conf(self: Any, f: Any) -> None:
        if resolv_conf is None:
            raise dns.resolver.NoResolverConfiguration("cannot open /etc/resolv.conf")
        original_read(self, io.StringIO(resolv_conf))

    async def run() -> Any:
        await server.start()
        try:
            check = Check(
                check_id=1, service_id=1, name="c", check_type="dns", url=url, data=data
            )
            return await DnsCheckExecutor().execute(check)
        finally:
            server.close()

    with (
        mock.patch.object(dns.resolver.BaseResolver, "reset", reset),
        mock.patch.object(
            dns.resolver.BaseResolver, "read_resolv_conf", read_resolv_conf
        ),
    ):
        result = asyncio.run(run())
    result_data = dict(result.data)
    keys = list(result_data)
    if "query_time_ms" in result_data:
        result_data["query_time_ms"] = "<ms>"
    message = str(result_data.get("error_msg", ""))
    if message.startswith("All nameservers failed"):
        result_data["error_msg"] = "<P2>"
    elif "error_msg" in result_data:
        result_data["error_msg"] = _normalise_set_reprs(message)
    return {
        "url": url,
        "data": data,
        "resolv_conf": resolv_conf,
        "steps": steps,
        "outcome": {"status": str(result.status), "keys": keys, "data": result_data},
        "requests": server.requests,
    }


def _executor_scenarios() -> list[dict[str, Any]]:
    name = "www.example.test"
    a_ok = _response(name, "A", answer=[(name + ".", "A", "192.0.2.1")])
    base = {"expected_ips": ["192.0.2.1"], "dns_server": _SERVER, "timeout": 2}
    fast = {**base, "timeout": 0.2}

    def case(
        steps: list[dict[str, Any]],
        data: Any = None,
        url: str = name,
        resolv_conf: str | None = _DEFAULT_RESOLV,
    ) -> dict[str, Any]:
        return {
            "url": url,
            "data": base if data is None else data,
            "steps": steps,
            "resolv_conf": resolv_conf,
        }

    mx = _response(
        name,
        "MX",
        answer=[
            (name + ".", "MX", "10 mail.example.test."),
            (name + ".", "MX", "20 Backup.Example.Test."),
        ],
    )
    txt = _response(
        name,
        "TXT",
        answer=[
            (name + ".", "TXT", '"v=spf1" "-all"'),
            (name + ".", "TXT", '"caf\\195\\169 \\"quoted\\""'),
            (name + ".", "TXT", '""'),
        ],
    )
    cname_chain = _response(
        name,
        "A",
        answer=[
            (name + ".", "CNAME", "alias.example.test."),
            ("alias.example.test.", "CNAME", "final.example.test."),
            ("final.example.test.", "A", "192.0.2.1"),
            ("final.example.test.", "A", "192.0.2.9"),
            ("other.example.test.", "A", "192.0.2.77"),
        ],
    )
    return [
        case([_reply(a_ok)]),
        case([_reply(a_ok)], data={**base, "expected_ips": ["192.0.2.2"]}),
        case([_reply(a_ok)], data={**base, "source_ip": "127.0.0.1"}),
        case([_reply(a_ok)], data={**base, "dns_server": ""}),
        case([_reply(a_ok)], data={**base, "dns_server": None}),
        case(
            [_reply(a_ok)],
            data={k: v for k, v in base.items() if k != "dns_server"},
            resolv_conf="# comment\n; also\nnameserver\nsearch example.test\noptions rotate timeout:1 ndots:2\nnameserver 127.0.0.1\n",
        ),
        case(
            [_reply(a_ok)],
            data={k: v for k, v in base.items() if k != "dns_server"},
            resolv_conf="options edns0\nnameserver 127.0.0.1\n",
        ),
        case([_reply(a_ok)], resolv_conf="options edns0\nnameserver 127.0.0.1\n"),
        case([_reply(a_ok)], resolv_conf="search example.test\n"),
        case([_reply(a_ok)], resolv_conf=None),
        case([_reply(a_ok)], resolv_conf="nameserver not-an-address\n"),
        case([_reply(a_ok)], data={**base, "dns_server": 5}),
        case([_reply(a_ok)], url="WWW.Example.TEST."),
        case(
            [
                _reply(
                    _response(
                        "WWW.Example.TEST",
                        "A",
                        answer=[("www.example.test.", "A", "192.0.2.1")],
                    )
                )
            ],
            url="WWW.Example.TEST",
        ),
        case([_reply(a_ok)], url="a..b"),
        case([_reply(a_ok)], url="x\\"),
        case([_reply(a_ok)], url="a" * 64),
        case(
            [
                _reply(
                    _response(
                        "a\\.b c", "A", answer=[("a\\.b\\032c.", "A", "192.0.2.1")]
                    )
                )
            ],
            url="a\\.b c",
        ),
        case(
            [
                _reply(
                    _response(
                        name,
                        "AAAA",
                        answer=[
                            (name + ".", "AAAA", "2a01:4f8:0:0::1"),
                            (name + ".", "AAAA", "::ffff:192.0.2.1"),
                            (name + ".", "AAAA", "1:0:0:1:0:0:0:1"),
                        ],
                    )
                )
            ],
            data={**base, "query_type": "AAAA", "expected_ips": ["2A01:04F8::0001"]},
        ),
        case(
            [_reply(mx)],
            data={**base, "query_type": "MX", "expected_ips": ["mail.example.test."]},
        ),
        case(
            [_reply(mx)],
            data={
                **base,
                "query_type": "MX",
                "expected_ips": ["10 mail.example.test."],
            },
        ),
        case(
            [_reply(txt)],
            data={**base, "query_type": "TXT", "expected_ips": ["v=spf1 -all"]},
        ),
        case(
            [
                _reply(
                    _response(
                        name, "TXT", answer=[(name + ".", "TXT", '"ok" "\\255bad"')]
                    )
                )
            ],
            data={**base, "query_type": "TXT", "expected_ips": ["x"]},
        ),
        case(
            [
                _reply(
                    _response(
                        name,
                        "CNAME",
                        answer=[(name + ".", "CNAME", "Target.Example.test.")],
                    )
                )
            ],
            data={
                **base,
                "query_type": "CNAME",
                "expected_ips": ["target.example.test."],
            },
        ),
        case(
            [
                _reply(
                    _response(
                        name,
                        "NS",
                        answer=[
                            (name + ".", "NS", "ns1.example.test."),
                            (name + ".", "NS", "ns2.example.test."),
                        ],
                    )
                )
            ],
            data={**base, "query_type": "NS", "expected_ips": ["ns2.example.test."]},
        ),
        case(
            [
                _reply(
                    _response(
                        name,
                        "SOA",
                        answer=[
                            (
                                name + ".",
                                "SOA",
                                "ns1.example.test. hostmaster.example.test. 2024010101 7200 3600 1209600 300",
                            )
                        ],
                    )
                )
            ],
            data={**base, "query_type": "SOA", "expected_ips": ["x"]},
        ),
        case(
            [
                _reply(
                    _response(
                        "1.2.0.192.in-addr.arpa",
                        "PTR",
                        answer=[
                            ("1.2.0.192.in-addr.arpa.", "PTR", "host.example.test.")
                        ],
                    )
                )
            ],
            url="1.2.0.192.in-addr.arpa",
            data={**base, "query_type": "PTR", "expected_ips": ["host.example.test."]},
        ),
        case([_reply(cname_chain)]),
        case(
            [
                _reply(
                    _response(
                        name, "A", answer=[(name + ".", "CNAME", "alias.example.test.")]
                    )
                )
            ],
        ),
        case(
            [
                _reply(
                    _response(
                        name,
                        "A",
                        rcode="NXDOMAIN",
                        answer=[(name + ".", "CNAME", "gone.example.test.")],
                    )
                )
            ],
        ),
        case([_reply(_response(name, "A", rcode="NXDOMAIN"))]),
        case(
            [
                _reply(
                    _response(
                        name,
                        "A",
                        rcode="NXDOMAIN",
                        answer=[(name + ".", "A", "192.0.2.1")],
                    )
                )
            ]
        ),
        case(
            [
                _reply(
                    _response(
                        name,
                        "A",
                        authority=[("example.test.", "SOA", "ns. host. 1 2 3 4 5")],
                    )
                )
            ],
        ),
        case([_reply(_response(name, "A", answer=[(name + ".", "AAAA", "::1")]))]),
        case([_reply(_response(name, "A", rcode="SERVFAIL"))]),
        case([_reply(_response(name, "A", rcode="REFUSED", no_question=True))]),
        case([_reply(_response(name, "A", rcode="YXDOMAIN"))]),
        case(
            [_reply(_response(name, "A", rcode="SERVFAIL")), _reply(a_ok)],
            data={k: v for k, v in base.items() if k != "dns_server"},
            resolv_conf="nameserver 127.0.0.1\nnameserver 127.0.0.1\n",
        ),
        case([_reply(_response(name, "A", tc=True)), _reply(a_ok)]),
        case(
            [
                _reply(_response(name, "A", tc=True)),
                _reply(
                    _response(
                        name, "A", tc=True, answer=[(name + ".", "A", "192.0.2.1")]
                    )
                ),
            ]
        ),
        case([_reply(_response(name, "A", tc=True)), _CLOSE]),
        case([_reply("00ff", a_ok)]),
        case([_reply(a_ok, id_mode="other"), _DROP], data=fast),
        case(
            [
                _reply(
                    _response(
                        "other.example.test",
                        "A",
                        answer=[("other.example.test.", "A", "192.0.2.1")],
                    ),
                    a_ok,
                )
            ]
        ),
        case(
            [_reply(_response(name, "MX", answer=[(name + ".", "MX", "1 x.")]), a_ok)]
        ),
        case([_reply(_response(name, "A", qr=False), a_ok)]),
        case([_reply(a_ok + "00", a_ok)]),
        case([_DROP], data=fast),
        case([_DROP], data={**fast, "source_ip": 5}),
        case(
            [
                _reply(
                    _response(
                        name,
                        "A",
                        answer=[
                            (name + ".", "A", "192.0.2.1"),
                            (name + ".", "A", "192.0.2.1"),
                            (name + ".", "A", "192.0.2.3"),
                        ],
                    )
                )
            ],
            data={**base, "expected_ips": ["192.0.2.3"]},
        ),
        case(
            [
                _reply(
                    _response(
                        name, "A", answer=[(name + ".", "A", "192.0.2.1")], edns=True
                    )
                )
            ],
            resolv_conf="options edns0\nnameserver 127.0.0.1\n",
        ),
        case([_reply(a_ok)], data={**base, "timeout": 0}),
        case([_reply(a_ok)], data={**base, "query_type": "BAD"}),
        case([_reply(a_ok)], data={**base, "query_type": ["A"]}),
        case([_reply(a_ok)], data=["expected_ips"]),
        case([_reply(a_ok)], data=5),
    ]


def build_config_dns() -> dict[str, Any]:
    return {
        "cases": [
            {"data": data, "outcome": _config_outcome(data)}
            for data in _config_corpus()
        ],
        "executor_cases": [
            _run_executor_case(**spec) for spec in _executor_scenarios()
        ],
    }


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "config_dns.json": build_config_dns,
    "dns_text.json": build_dns_text,
}
