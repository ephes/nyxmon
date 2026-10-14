"""Fixture builders of slice S5b (DNS check), loaded by ``generate.py``.

* ``config_dns.json``: ``DnsCheckConfig.from_dict`` + ``validate`` outcomes
  for a data corpus (``cases``).
* ``dns_text.json``: ``normalize_dns_value``, ``expected_ip_error``,
  ``_check_ip_match``, ``dns.name.from_text`` + ``to_text`` (the query
  name), dnspython's IPv6 text form and ``bytes.decode()`` errors of TXT
  strings.
"""

from __future__ import annotations

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


def build_config_dns() -> dict[str, Any]:
    return {
        "cases": [
            {"data": data, "outcome": _config_outcome(data)}
            for data in _config_corpus()
        ],
    }


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "config_dns.json": build_config_dns,
    "dns_text.json": build_dns_text,
}
