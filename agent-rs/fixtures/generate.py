"""Generate the shared fixtures of the Rust agent from the Python code.

The Python implementation is the oracle (agent-rs/PLAN.md section 7.2): every
fixture file is produced by calling the real Python functions on a fixed
corpus, with clocks, nonces and the environment injected. The Rust tests load
the files with ``include_str!``; ``tests/unit/test_agent_rs_fixtures.py``
regenerates them in memory and fails when a committed file differs.

Usage::

    uv run python agent-rs/fixtures/generate.py          # rewrite every file
    uv run python agent-rs/fixtures/generate.py --check  # exit 1 on drift

Each slice adds its fixture builders to ``GENERATORS``.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import math
import os
import random
import struct
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

FIXTURES_DIR = Path(__file__).resolve().parent

# ----------------------------------------------------------------- helpers


def _float_bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def _bits_float(bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(bits))[0]


def _error(exc: BaseException) -> dict[str, str]:
    return {"class": type(exc).__name__, "message": str(exc)}


def _has_surrogate(value: Any) -> bool:
    """Whether a decoded value holds a lone surrogate (iterative: deep docs)."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                return True
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
    return False


def _loads_case(parse: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = parse()
    except RecursionError:
        # The message names the container type at the failing depth; only
        # the class is part of the contract.
        return {"error": {"class": "RecursionError", "message": ""}, **_no_value()}
    except ValueError as exc:
        return {"error": _error(exc), **_no_value()}
    return {
        "error": None,
        "dumps": json.dumps(value),
        "lone_surrogate": _has_surrogate(value),
    }


def _no_value() -> dict[str, Any]:
    return {"dumps": None, "lone_surrogate": False}


# ------------------------------------------------------------------ pyjson

DUMP_OPTIONS: dict[str, dict[str, Any]] = {
    # sqlite_repo.py storage of check_result.data / collector_incident.payload
    "python_default": {},
    # json_metrics_executor.py:_bounded_actual
    "bounded_actual": {"ensure_ascii": False},
    # notification.py:_build_opsgate_prompt untrusted block
    "opsgate_block": {"indent": 2, "sort_keys": True, "ensure_ascii": True},
    # httpx 0.28 json= request bodies
    "httpx_json": {"ensure_ascii": False, "separators": (",", ":"), "allow_nan": False},
}

SPECIAL_FLOATS = [
    0.0,
    -0.0,
    0.1,
    0.5,
    1.0,
    1.5,
    2.5,
    -2.5,
    0.125,
    0.375,
    2.675,
    1.005,
    0.0001,
    0.00001,
    0.000123456,
    123456.0,
    1234567.0,
    999999.5,
    9999995.0,
    1e15,
    1e16,
    1.5e16,
    1e17,
    1e22,
    1e-7,
    123.456,
    -123.456,
    1 / 3,
    2 / 3,
    math.pi,
    math.e,
    5e-324,
    2.2250738585072014e-308,
    1.7976931348623157e308,
    9007199254740992.0,
    9007199254740993.0,
    0.30000000000000004,
    100.0,
    1e100,
    12345.6789,
    0.05,
    0.15,
    0.25,
    0.35,
    0.45,
    1.45,
    2.45,
    1234.5,
    1234.55,
    99.95,
    float("nan"),
    float("inf"),
    float("-inf"),
]


def _float_corpus() -> list[float]:
    rng = random.Random(20261014)
    values = list(SPECIAL_FLOATS)
    for _ in range(300):
        values.append(_bits_float(f"{rng.getrandbits(64):016x}"))
    # Exact quarter values just above 2**49 need 17 digits; their two 16-digit
    # neighbours are equally near, so repr() must break the tie to even.
    for _ in range(40):
        values.append(rng.randint(2**49, 2**50) + rng.choice([0.25, 0.75]))
    for _ in range(100):
        values.append(round(rng.uniform(-1e6, 1e6), rng.randint(0, 6)))
    for _ in range(50):
        values.append(rng.uniform(0, 1) * 10 ** rng.randint(-12, 20))
    return values


def _float_cases() -> list[dict[str, Any]]:
    cases = []
    for value in _float_corpus():
        case: dict[str, Any] = {
            "bits": _float_bits(value),
            "repr": repr(value),
            "g": format(value, "g"),
        }
        for digits in (0, 1, 2, 3):
            case[f"round{digits}"] = repr(round(value, digits))
        cases.append(case)
    return cases


def _value_corpus() -> list[str]:
    """JSON documents covering every value shape the agent stores."""
    rng = random.Random(4300)
    strings = [
        "",
        "a",
        "plain text",
        'quote " inside',
        "single ' quote",
        "back\\slash",
        "slash /",
        "tab\tnew\nline\rcr",
        "\b\f",
        "\x00\x01\x1f",
        "\x7f",
        "caf\u00e9",
        "\u00ff\u0100",
        "\u2028\u2029",
        "\ufeff",
        "\ue000",
        "emoji \U0001f600",
        "\U0010ffff",
        "mixed \u65e5\u672c \U0001f680 end",
        "`backtick`",
        "<html>&amp;</html>",
    ]
    ints = [
        0,
        1,
        -1,
        42,
        255,
        256,
        -5,
        2**31,
        2**53,
        2**53 + 1,
        2**63 - 1,
        2**63,
        -(2**63),
        -(2**63) - 1,
        10**30,
        -(10**40),
        int("9" * 4300),
    ]
    docs: list[str] = ["null", "true", "false"]
    docs += [json.dumps(s) for s in strings]
    docs += [json.dumps(s, ensure_ascii=False) for s in strings]
    docs += [str(i) for i in ints]
    docs += [json.dumps(f) for f in SPECIAL_FLOATS]
    docs += [json.dumps(rng.uniform(-1e9, 1e9)) for _ in range(40)]
    docs += [str(rng.randint(-(10**25), 10**25)) for _ in range(20)]
    docs += [
        "[]",
        "{}",
        "[[]]",
        "[{}]",
        '{"a": []}',
        '{"a": {}}',
        "[1, 2.0, true, null]",
        '["x", ["y", ["z"]]]',
        '{"b": 1, "a": 2}',
        '{"z": {"y": {"x": [1, {"w": null}]}}}',
        '{"a": 1, "a": 2}',
        '{"a": 1, "b": 2, "a": 3}',
        '{"\u00e9": "\u00e9", "e": "e", "E": "E", "_": 1}',
        '{"": 0}',
        '{"key with space": [NaN, Infinity, -Infinity]}',
        "[1e400, -1e400, 1E2, 1e-2, -0, -0.0, 0e0]",
        '{"nested": [[[[[[[[[[1]]]]]]]]]]}',
        "[" + ", ".join(str(n) for n in range(30)) + "]",
        json.dumps({f"k{n}": n for n in range(30)}),
        '{"error_type": "http_error", "error_msg": "HTTP 503", "status_code": 503}',
        (
            '{"failures": [{"path": "$.a", "op": ">", "expected": 1, "actual": 0.5,'
            ' "severity": "critical"}], "duration_ms": 12}'
        ),
    ]
    for _ in range(110):
        docs.append(json.dumps(_random_value(rng, 3)))
    return docs


def _random_value(rng: random.Random, depth: int) -> Any:
    kind = rng.randint(0, 7 if depth > 0 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.random() < 0.5
    if kind == 2:
        return rng.randint(-(10**20), 10**20)
    if kind == 3:
        return rng.uniform(-1e5, 1e5)
    if kind in (4, 5):
        return "".join(
            chr(rng.choice([rng.randint(0x20, 0x7E), rng.randint(0, 0x2FFF), 0x1F600]))
            for _ in range(rng.randint(0, 8))
        )
    if kind == 6:
        return [_random_value(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    return {
        f"k{rng.randint(0, 99)}": _random_value(rng, depth - 1)
        for _ in range(rng.randint(0, 4))
    }


INVALID_DOCS = [
    "",
    " ",
    "  \n ",
    "x",
    " x",
    "nul",
    "tru",
    "fals",
    "Nan",
    "NaN1",
    "inf",
    "-Inf",
    "-",
    "-x",
    "+1",
    "01",
    "-01",
    "1.",
    "1.e5",
    ".5",
    "1e",
    "1e+",
    "1E-",
    "0x10",
    "1 2",
    "[",
    "]",
    "{",
    "}",
    "[1",
    "[1,",
    "[1,]",
    "[1 2]",
    "[,1]",
    "[1,,2]",
    '{"a"',
    '{"a":',
    '{"a":1',
    '{"a":1,}',
    '{"a" 1}',
    "{1:2}",
    "{,}",
    '{"a":1 "b":2}',
    "{'a': 1}",
    '"abc',
    '"\\',
    '"a\x01"',
    '"tab\tinside"',
    '"line\nbreak"',
    '"\\x"',
    '"\\u12"',
    '"\\u12zz"',
    '"\\u123"',
    '"\\ud800\\uzzzz"',
    '"\\ud800\\u12"',
    "\ufeff1",
    "[1]\n\n  x",
    '{\n  "a": 1,\n  "b" 2\n}',
    "1\x00",
    "[" * 20000 + "]" * 20000,
    '{"a":' * 20000 + "1" + "}" * 20000,
    "1" * 4301,
    "-" + "1" * 4301,
    "[" + "1" * 4301 + ", x]",
    "[x, " + "1" * 4301 + "]",
]

EXTRA_VALID_DOCS = [
    " \t\n\r1 \t\n\r",
    "1e400",
    "-1e400",
    "1E400",
    "1e-400",
    "123456789012345678901234567890",
    "1" * 4300,
    "-" + "1" * 4300,
    "[" * 500 + "]" * 500,
    '{"a":' * 500 + "1" + "}" * 500,
    '"\\ud83d\\ude00"',
    '"\\uD83D\\uDE00"',
    '"\\ud800"',
    '"\\udc00"',
    '"x\\ud800\\u0041"',
    '"\\ud800\\ud800"',
    '["\\ud800", 1]',
    '{"\\ud800": 1}',
    '"\\/\\b\\f\\n\\r\\t\\"\\\\"',
    '"\\u0000\\u001f\\u007f"',
    '"\x7f\u0080\u2028"',
    "NaN",
    "Infinity",
    "-Infinity",
    "[NaN,Infinity,-Infinity]",
    "-0",
    "-0.0",
    "0.0e0",
    "1.5E+3",
    "2e-3",
]

BYTES_DOCS = [
    b'{"a": 1}',
    b'\xef\xbb\xbf{"a": 1}',
    b'\xef\xbb\xbf\xef\xbb\xbf{"a": 1}',
    '{"k": "caf\u00e9 \U0001f600"}'.encode("utf-16-le"),
    '{"k": "caf\u00e9 \U0001f600"}'.encode("utf-16-be"),
    '{"k": "caf\u00e9 \U0001f600"}'.encode("utf-16"),
    b"\xfe\xff" + '{"k": 1}'.encode("utf-16-be"),
    '{"k": "caf\u00e9 \U0001f600"}'.encode("utf-32-le"),
    '{"k": "caf\u00e9 \U0001f600"}'.encode("utf-32-be"),
    '{"k": "caf\u00e9 \U0001f600"}'.encode("utf-32"),
    b"\x00\x00\xfe\xff" + "[1]".encode("utf-32-be"),
    "1".encode("utf-16-le"),
    "1".encode("utf-16-be"),
    b"12",
    b"1",
    b"",
    b"[1, 2]",
    b'"\xc3\xa9"',
    b'"\xff"',
    b'"\xc3"',
    b'"\xed\xa0\x80"',
    b"\xed\xa0\x80",
    b'"\xed\xa0\x80\xed\xb0\x80"',
    b'["\xe2\x82\xac", 1.5]',
    '"\ud800"'.encode("utf-16-le", "surrogatepass"),
    '"\ud800"'.encode("utf-32-le", "surrogatepass"),
    b"[1]\x00",
    b"\x00",
    b"\x01\x00\x00\x00",
    b"[1]\x00\x00\x00",
    "[1]".encode("utf-16-le") + b"\x00",
    "[1]".encode("utf-32-le") + b"\x00",
    b"\xff\xfe\x00\x00" + b"\x00\x00\x11\x00",
    b'{"a": NaN, "b": 1e999}',
    b"\xef\xbb\xbf",
]

COMPARE_DOCS = [
    "null",
    "true",
    "false",
    "0",
    "1",
    "-1",
    "2",
    "9223372036854775808",
    "-9223372036854775809",
    "1000000000000000000000000000000",
    "9007199254740993",
    "0.0",
    "-0.0",
    "0.5",
    "1.0",
    "1.5",
    "2.0",
    "9007199254740992.0",
    "1e30",
    "1e16",
    "NaN",
    "Infinity",
    "-Infinity",
    '""',
    '"a"',
    '"b"',
    '"ab"',
    '"B"',
    '"\\u00e9"',
    '"\\ud83d\\ude00"',
    "[]",
    "[1]",
    "[1, 2]",
    "[1.0]",
    "[2]",
    '[1, "a"]',
    '["a", 1]',
    "[NaN]",
    "[1, NaN]",
    "[[1]]",
    "[null]",
    "{}",
    '{"a": 1}',
    '{"a": 1.0}',
    '{"a": true}',
    '{"a": NaN}',
    '{"b": 1}',
    '{"a": 1, "b": 2}',
    '{"b": 2, "a": 1}',
]

INT_TEXTS = [
    "a" * 199,
    "a" * 250,
    "\u00e9" * 300,
    "0",
    "1",
    "-1",
    "+1",
    "007",
    "-007",
    "1_000",
    "1__000",
    "_1",
    "1_",
    "+_1",
    "-_1",
    "0_0",
    " 42 ",
    "\t42\n",
    "\x1c42\x1f",
    "\u00a042\u3000",
    "\u200b42",
    "4 2",
    "",
    " ",
    "+",
    "-",
    "1.0",
    "1e3",
    "0x10",
    "abc",
    "9223372036854775807",
    "9223372036854775808",
    "-9223372036854775809",
    "1" * 4300,
    "1" * 4301,
    "-" + "1" * 4301,
    "1_" * 4300 + "1",
    "1_" * 4299 + "1",
    "--1",
    "+-1",
    "1-",
]

FLOAT_TEXTS = [
    "a" * 250,
    "0",
    "1",
    "-1.5",
    "+2.5",
    "1.",
    ".5",
    "-.5",
    ".",
    "1e5",
    "1E5",
    "1e+5",
    "1e-5",
    "1e",
    "e5",
    "1e5.0",
    "1_000.5",
    "1_000.0_5",
    "1e1_0",
    "1__0",
    "_1",
    "1_",
    "1_.5",
    "1._5",
    "1_e5",
    "1e_5",
    "inf",
    "-inf",
    "+inf",
    "Inf",
    "INFINITY",
    "infinity",
    "-Infinity",
    "infinit",
    "nan",
    "-nan",
    "NaN",
    "nan(1)",
    "1e400",
    "-1e400",
    "1e-400",
    " 3.25 ",
    "\u00a03.25",
    "\x1c3.25",
    "",
    " ",
    "abc",
    "0x10",
    "1,5",
    "30",
    "30.0000001",
    "29.999",
    "5",
    "0.0",
    "-0",
    "1_0",
]

STR_REPR_TEXTS = [
    # Assigned in Unicode 16 (non-printable for CPython 3.13 / Unicode 15.1)
    # and in Unicode 15.1 (CJK Extension I), around the version boundary.
    "\U0001fae9 \U0002ebf0 \U0002ee5d \U0002ee5e \u1c89 \U00016100",
    "",
    "abc",
    "it's",
    'say "hi"',
    "both ' and \"",
    "back\\slash",
    "tab\tnl\ncr\r",
    "\x00\x07\x1b\x7f",
    "\x80\x9f\xa0\xad\xff",
    "caf\u00e9",
    "\u2028\u2029\u200b\u3000",
    "\ue000\U000f0000",
    "\U0001f600",
    "\u0378",
    "\ufeff",
    "observe ",
    "  OBSERVE  ",
    "\U000e0001",
    "\u1680x",
    "\x1c\x1d\x1e\x1f",
    "\x85text\x85",
    "\u00a0nbsp",
    "\u180e",
    "\u65e5\u672c",
]

BYTES_REPR_DATA = [
    b"",
    b"abc",
    b"it's",
    b'say "hi"',
    b"both ' and \"",
    b"back\\slash",
    b"\t\n\r",
    b"\x00\x7f\x80\xff",
    b"[AUTHENTICATIONFAILED] Authentication failed.",
    "caf\u00e9".encode(),
]


def _compare_code(a: Any, b: Any, op: Callable[[Any, Any], Any]) -> str:
    try:
        return "T" if op(a, b) else "F"
    except TypeError:
        return "E"


def _int_of(value: Any) -> dict[str, Any]:
    try:
        return {"int": str(int(value)), "int_error": None}
    except (TypeError, ValueError, OverflowError) as exc:
        return {"int": None, "int_error": type(exc).__name__}


def _json_kind(value: Any) -> str:
    from nyxmon.service_layer.notification_suppression import _json_kind

    return _json_kind(value)


def build_pyjson() -> dict[str, Any]:
    import operator

    ops = [
        ("==", operator.eq),
        ("!=", operator.ne),
        ("<", operator.lt),
        ("<=", operator.le),
        (">", operator.gt),
        (">=", operator.ge),
    ]

    values = _value_corpus()
    value_cases = []
    for doc in values:
        value = json.loads(doc)
        case: dict[str, Any] = {
            "doc": doc,
            "truthy": bool(value),
            "kind": _json_kind(value),
            **_int_of(value),
        }
        dumped: dict[str, Any] = {}
        for name, options in DUMP_OPTIONS.items():
            try:
                dumped[name] = {"ok": json.dumps(value, **options), "error": None}
            except ValueError as exc:
                dumped[name] = {"ok": None, "error": _error(exc)}
        case["dumps"] = dumped
        value_cases.append(case)

    loads_cases = []
    for doc in values + INVALID_DOCS + EXTRA_VALID_DOCS:
        case = {"doc": doc}
        case.update(_loads_case(functools.partial(json.loads, doc)))
        loads_cases.append(case)

    bytes_cases = []
    for data in BYTES_DOCS:
        case = {"hex": data.hex()}
        case.update(_loads_case(functools.partial(json.loads, data)))
        bytes_cases.append(case)

    compare_cases = []
    for a_doc in COMPARE_DOCS:
        for b_doc in COMPARE_DOCS:
            # Parse both sides separately, as the agent does: only the NaN /
            # Infinity constants of the json module are shared objects.
            a, b = json.loads(a_doc), json.loads(b_doc)
            codes = "".join(_compare_code(a, b, op) for _, op in ops)
            compare_cases.append({"a": a_doc, "b": b_doc, "ops": codes})

    int_cases = []
    for text in INT_TEXTS:
        try:
            int_cases.append({"text": text, "value": str(int(text)), "error": None})
        except ValueError as exc:
            int_cases.append({"text": text, "value": None, "error": _error(exc)})

    float_parse_cases = []
    for text in FLOAT_TEXTS:
        try:
            parsed = float(text)
        except ValueError as exc:
            float_parse_cases.append({"text": text, "bits": None, "error": _error(exc)})
            continue
        bits = "nan" if math.isnan(parsed) else _float_bits(parsed)
        float_parse_cases.append({"text": text, "bits": bits, "error": None})

    str_cases = [
        {"text": text, "repr": repr(text), "strip": text.strip(), "len": len(text)}
        for text in STR_REPR_TEXTS
    ]
    bytes_repr_cases = [
        {"hex": data.hex(), "repr": repr(data)} for data in BYTES_REPR_DATA
    ]

    return {
        "compare_ops": [symbol for symbol, _ in ops],
        "compare": compare_cases,
        "floats": _float_cases(),
        "float_parse": float_parse_cases,
        "int_parse": int_cases,
        "loads": loads_cases,
        "loads_bytes": bytes_cases,
        "str_repr": str_cases,
        "bytes_repr": bytes_repr_cases,
        "values": value_cases,
    }


# --------------------------------------------------------------------- env


@contextmanager
def _captured_warnings() -> Iterator[list[list[str]]]:
    records: list[list[str]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                records.append([record.name, record.getMessage()])

    handler = _Handler(level=logging.WARNING)
    logger = logging.getLogger("nyxmon")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _reset_env_state() -> None:
    from nyxmon.adapters import collector, site_connectivity
    from nyxmon.adapters.repositories import interface
    from nyxmon.service_layer import handlers, notification_policy

    interface._check_batch_size_from_value.cache_clear()
    collector._processing_lease_seconds_from_value.cache_clear()
    collector._shutdown_grace_from_value.cache_clear()
    notification_policy._bounded_env_value.cache_clear()
    notification_policy.reset_policy_warning_state()
    handlers._positive_env_value.cache_clear()
    handlers._delivery_retry_from_value.cache_clear()
    site_connectivity.reset_site_config_warning_state()


def _site_targets(targets: Any) -> list[list[Any]]:
    return [[t.path, t.kind, t.host, t.port, str(t)] for t in targets]


def _read_site_config() -> dict[str, Any]:
    from nyxmon.adapters.site_connectivity import SiteConnectivityConfig

    config = SiteConnectivityConfig.from_env()
    return {
        "mode": config.mode.value,
        "probe_interval": config.probe_interval,
        "probe_timeout": config.probe_timeout,
        "ipv4_targets": _site_targets(config.ipv4_targets),
        "ipv6_targets": _site_targets(config.ipv6_targets),
        "dns_names": _site_targets(config.dns_names),
        "down_after_failures": config.down_after_failures,
        "recovery_grace_seconds": config.recovery_grace_seconds,
        "max_hold_seconds": config.max_hold_seconds,
        "incident_notify_after_seconds": config.incident_notify_after_seconds,
        "incident_reminder_seconds": config.incident_reminder_seconds,
        "stale_after_seconds": config.stale_after_seconds,
    }


# The missing-credentials warning of the notifier constructor belongs to the
# Telegram notifier (slice S8), not to the environment readers.
_NOTIFIER_CONSTRUCTOR_WARNING = "Telegram notifier initialized without token or chat_id"


def _read_notifier_config() -> dict[str, Any]:
    from nyxmon.adapters.notification import AsyncTelegramNotifier

    try:
        notifier = AsyncTelegramNotifier()
    except (OverflowError, ValueError) as exc:
        # float() of an oversized timeout fails the constructor (and startup).
        return {"error": type(exc).__name__}
    return {
        "telegram_token": notifier.token,
        "telegram_chat_id": notifier.chat_id,
        "opsgate_submit_base_url": notifier.opsgate_submit_base_url,
        "opsgate_submit_token": notifier.opsgate_submit_token,
        "opsgate_approval_base_url": notifier.opsgate_approval_base_url,
        "opsgate_ticket_expires_seconds": notifier.opsgate_ticket_expires_seconds,
        "opsgate_submit_timeout_seconds": repr(notifier.opsgate_submit_timeout_seconds),
        "opsgate_include_warnings": notifier.opsgate_include_warnings,
        "opsgate_enabled": notifier._opsgate_enabled(),
    }


def _env_readers() -> dict[str, Callable[[], Any]]:
    from nyxmon.adapters import collector
    from nyxmon.adapters.repositories import interface
    from nyxmon.service_layer import handlers, notification_policy

    return {
        "check_batch_size": interface.check_batch_size,
        "processing_lease_seconds": collector.processing_lease_seconds,
        # The default is the int 5, parsed values are floats; the value is only
        # used as a sleep duration, so it is compared as a float.
        "shutdown_grace_seconds": lambda: repr(
            float(collector.shutdown_grace_seconds())
        ),
        "global_consecutive_failures": notification_policy.global_consecutive_failures,
        "global_reminder_seconds:error": lambda: (
            notification_policy.global_reminder_seconds("error")
        ),
        "global_reminder_seconds:warning": lambda: (
            notification_policy.global_reminder_seconds("warning")
        ),
        "notify_immediate_cooldown_seconds": handlers._notify_immediate_cooldown_seconds,
        "delivery_retry_seconds": handlers.delivery_retry_seconds,
        "site_config": _read_site_config,
        "notifier_config": _read_notifier_config,
    }


INT_VALUES = [
    None,
    "",
    "   ",
    "abc",
    "1.5",
    "0",
    "-1",
    "1",
    "29",
    "30",
    " 42 ",
    "+7",
    "1_0",
    "60",
    "100",
    "101",
    "3600",
    "3601",
    "59",
    "2592000",
    "2592001",
    "99999999999999999999999",
    "-99999999999999999999999",
]

FLOAT_VALUES = [
    None,
    "",
    "0",
    "2.5",
    "30",
    "30.5",
    "-0.1",
    "nan",
    "inf",
    "-inf",
    "1e999",
    "abc",
    " 7 ",
    "1_0.5",
    "0x1",
]

SITE_ENV_CASES: list[dict[str, str]] = [
    {},
    {"NYXMON_SITE_CONNECTIVITY_MODE": "observe"},
    {"NYXMON_SITE_CONNECTIVITY_MODE": "  ENFORCE "},
    {"NYXMON_SITE_CONNECTIVITY_MODE": "off"},
    {"NYXMON_SITE_CONNECTIVITY_MODE": ""},
    {"NYXMON_SITE_CONNECTIVITY_MODE": "Shadow"},
    {"NYXMON_SITE_CONNECTIVITY_MODE": "obs'erve"},
    {
        "NYXMON_SITE_PROBE_INTERVAL_SECONDS": "15",
        "NYXMON_SITE_PROBE_TIMEOUT_SECONDS": "10",
    },
    {
        "NYXMON_SITE_PROBE_INTERVAL_SECONDS": "14",
        "NYXMON_SITE_PROBE_TIMEOUT_SECONDS": "0",
    },
    {
        "NYXMON_SITE_PROBE_INTERVAL_SECONDS": "abc",
        "NYXMON_SITE_PROBE_TIMEOUT_SECONDS": "11",
    },
    {
        "NYXMON_SITE_DOWN_AFTER_FAILURES": "10",
        "NYXMON_SITE_RECOVERY_GRACE_SECONDS": "60",
        "NYXMON_SITE_MAX_HOLD_SECONDS": "86400",
        "NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS": "86400",
        "NYXMON_SITE_INCIDENT_REMINDER_SECONDS": "2592000",
    },
    {
        "NYXMON_SITE_DOWN_AFTER_FAILURES": "0",
        "NYXMON_SITE_RECOVERY_GRACE_SECONDS": "3601",
        "NYXMON_SITE_MAX_HOLD_SECONDS": "599",
        "NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS": "59",
        "NYXMON_SITE_INCIDENT_REMINDER_SECONDS": "2592001",
    },
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": ""},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "   "},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1:8080"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": " 127.0.0.1:1 , ,10.0.0.1:65535, "},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": ","},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1:0"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1:65536"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1:+443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1: 443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1:4_43"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "127.0.0.1 :443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "01.1.1.1:443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "1.1.1:443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "256.1.1.1:443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "example.com:443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "[::1]:443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "[127.0.0.1]:443"},
    {"NYXMON_SITE_PROBE_IPV4_TARGETS": "1.1.1.1:443:1"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[::1]:443,[fe80::1%eth0]:22"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[::ffff:1.2.3.4]:443"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "::1:443"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[::1]443"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[::1"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[fe80::1%]:22"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[fe80::1%a%b]:22"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[fe80::1%a/b]:22"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[1.2.3.4]:443"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": "[2001:db8::1]:443,[2001:DB8::2]:80"},
    {"NYXMON_SITE_PROBE_IPV6_TARGETS": ""},
    {"NYXMON_SITE_PROBE_DNS_NAMES": "example.com, example.org"},
    {"NYXMON_SITE_PROBE_DNS_NAMES": ""},
    {"NYXMON_SITE_PROBE_DNS_NAMES": "exa mple.com"},
    {"NYXMON_SITE_PROBE_DNS_NAMES": "example.com/x"},
    {"NYXMON_SITE_PROBE_DNS_NAMES": ",,"},
    {"NYXMON_SITE_PROBE_DNS_NAMES": "a,\u00a0b"},
    {"NYXMON_SITE_PROBE_DNS_NAMES": "a\x1cb"},
    {
        "NYXMON_SITE_CONNECTIVITY_MODE": "enforce",
        "NYXMON_SITE_PROBE_IPV4_TARGETS": "",
        "NYXMON_SITE_PROBE_IPV6_TARGETS": "",
        "NYXMON_SITE_PROBE_DNS_NAMES": "",
    },
]

NOTIFIER_ENV_CASES: list[dict[str, str]] = [
    {},
    {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-100"},
    {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": " 42 "},
    {
        "OPSGATE_SUBMIT_BASE_URL": " https://opsgate.example/// ",
        "OPSGATE_SUBMIT_TOKEN": " secret ",
    },
    {
        "OPSGATE_SUBMIT_BASE_URL": "https://submit.example",
        "OPSGATE_SUBMIT_TOKEN": "t",
        "OPSGATE_APPROVAL_BASE_URL": "https://approve.example/",
    },
    {"OPSGATE_SUBMIT_BASE_URL": "https://submit.example", "OPSGATE_SUBMIT_TOKEN": "  "},
    {"OPSGATE_APPROVAL_BASE_URL": "  /  "},
    {"OPSGATE_TICKET_EXPIRES_SECONDS": "600", "OPSGATE_SUBMIT_TIMEOUT_SECONDS": "3"},
    {"OPSGATE_TICKET_EXPIRES_SECONDS": "abc", "OPSGATE_SUBMIT_TIMEOUT_SECONDS": "0"},
    {"OPSGATE_TICKET_EXPIRES_SECONDS": "-5", "OPSGATE_SUBMIT_TIMEOUT_SECONDS": "1.5"},
    {"OPSGATE_TICKET_EXPIRES_SECONDS": "  ", "OPSGATE_SUBMIT_TIMEOUT_SECONDS": ""},
    {
        "OPSGATE_TICKET_EXPIRES_SECONDS": "99999999999999999999999",
        "OPSGATE_SUBMIT_TIMEOUT_SECONDS": "99999999999999999999999",
    },
    {"OPSGATE_SUBMIT_TIMEOUT_SECONDS": "9007199254740993"},
    {
        "OPSGATE_SUBMIT_TIMEOUT_SECONDS": "1" + "0" * 400,
        "OPSGATE_TICKET_EXPIRES_SECONDS": "x",
    },
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": "1"},
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": " YES "},
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": "On"},
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": "true"},
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": "0"},
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": "no"},
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": ""},
    {"OPSGATE_SUBMIT_INCLUDE_WARNINGS": "y"},
]


def _env_scenarios() -> list[dict[str, Any]]:
    """Each scenario resets the warn-once state and runs its calls in order."""
    scenarios: list[dict[str, Any]] = []

    def single(reader: str, var: str, values: list[str | None]) -> None:
        for value in values:
            env = {} if value is None else {var: value}
            scenarios.append({"reader": reader, "calls": [env]})

    single("check_batch_size", "NYXMON_CHECK_BATCH_SIZE", INT_VALUES)
    single("processing_lease_seconds", "NYXMON_PROCESSING_LEASE_SECONDS", INT_VALUES)
    single("shutdown_grace_seconds", "NYXMON_SHUTDOWN_GRACE_SECONDS", FLOAT_VALUES)
    single(
        "global_consecutive_failures", "NYXMON_NOTIFY_CONSECUTIVE_FAILURES", INT_VALUES
    )
    for reader in ("global_reminder_seconds:error", "global_reminder_seconds:warning"):
        single(reader, "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS", INT_VALUES)
        single(reader, "NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS", INT_VALUES)
        single(reader, "NYXMON_NOTIFY_REPEAT_FAILURES", ["", " 3 ", "x"])
    single(
        "notify_immediate_cooldown_seconds",
        "NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS",
        INT_VALUES,
    )
    single("delivery_retry_seconds", "NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS", INT_VALUES)
    for env in SITE_ENV_CASES:
        scenarios.append({"reader": "site_config", "calls": [env]})
    for env in NOTIFIER_ENV_CASES:
        scenarios.append({"reader": "notifier_config", "calls": [env]})

    # Warn once per distinct (variable, value), for the process lifetime.
    scenarios += [
        {
            "reader": "check_batch_size",
            "calls": [
                {"NYXMON_CHECK_BATCH_SIZE": "abc"},
                {"NYXMON_CHECK_BATCH_SIZE": "abc"},
                {"NYXMON_CHECK_BATCH_SIZE": " abc "},
                {"NYXMON_CHECK_BATCH_SIZE": "xyz"},
                {"NYXMON_CHECK_BATCH_SIZE": "abc"},
            ],
        },
        {
            "reader": "processing_lease_seconds",
            "calls": [
                {"NYXMON_PROCESSING_LEASE_SECONDS": "10"},
                {"NYXMON_PROCESSING_LEASE_SECONDS": "10"},
                {"NYXMON_PROCESSING_LEASE_SECONDS": "11"},
                {"NYXMON_PROCESSING_LEASE_SECONDS": "x"},
                {"NYXMON_PROCESSING_LEASE_SECONDS": "x"},
            ],
        },
        {
            "reader": "shutdown_grace_seconds",
            "calls": [
                {"NYXMON_SHUTDOWN_GRACE_SECONDS": "31"},
                {"NYXMON_SHUTDOWN_GRACE_SECONDS": "31"},
                {"NYXMON_SHUTDOWN_GRACE_SECONDS": "nan"},
            ],
        },
        {
            "reader": "global_reminder_seconds:error",
            "calls": [
                {"NYXMON_NOTIFY_REPEAT_FAILURES": "3"},
                {"NYXMON_NOTIFY_REPEAT_FAILURES": "3"},
                {"NYXMON_NOTIFY_REPEAT_FAILURES": "4"},
                {
                    "NYXMON_NOTIFY_REPEAT_FAILURES": "3",
                    "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS": "5",
                },
                {
                    "NYXMON_NOTIFY_REPEAT_FAILURES": "3",
                    "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS": "5",
                },
            ],
        },
        {
            "reader": "delivery_retry_seconds",
            "calls": [
                {"NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS": "30"},
                {"NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS": "30"},
                {"NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS": "abc"},
            ],
        },
        {
            "reader": "notify_immediate_cooldown_seconds",
            "calls": [
                {"NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS": "0"},
                {"NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS": "0"},
            ],
        },
        {
            "reader": "site_config",
            "calls": [
                {"NYXMON_SITE_CONNECTIVITY_MODE": "bogus"},
                {"NYXMON_SITE_CONNECTIVITY_MODE": "BOGUS"},
                {"NYXMON_SITE_CONNECTIVITY_MODE": "other"},
                {"NYXMON_SITE_PROBE_IPV4_TARGETS": "x"},
                {"NYXMON_SITE_PROBE_IPV4_TARGETS": "x"},
            ],
        },
        {
            "reader": "notifier_config",
            "calls": [
                {"OPSGATE_TICKET_EXPIRES_SECONDS": "abc"},
                {"OPSGATE_TICKET_EXPIRES_SECONDS": "abc"},
            ],
        },
    ]
    return scenarios


def build_env() -> dict[str, Any]:
    readers = _env_readers()
    results = []
    for scenario in _env_scenarios():
        _reset_env_state()
        calls = []
        for env in scenario["calls"]:
            with mock.patch.dict(os.environ, env, clear=True):
                with _captured_warnings() as warnings:
                    value = readers[scenario["reader"]]()
            warnings = [w for w in warnings if w[1] != _NOTIFIER_CONSTRUCTOR_WARNING]
            calls.append({"env": env, "value": value, "warnings": warnings})
        results.append({"reader": scenario["reader"], "calls": calls})
    _reset_env_state()
    return {"scenarios": results}


IDNA_HOSTS = [
    "example.com",
    "Example.COM",
    "127.0.0.1",
    "xn--bcher-kva.example",
    "b\u00fccher.example",
    "B\u00dcCHER.example",
    "WWW.B\u00fccher.DE",
    "home.wersd\u00f6rfer.de",
    "stra\u00dfe.de",
    "\u03c3\u03af\u03c3\u03c5\u03c6\u03bf\u03c2.gr",
    "\u65e5\u672c\u8a9e\u3002jp",
    "\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45\uff0e\uff43\uff4f\uff4d",
    "b\u00fccher.example.",
    "caf\u00e9.com",
    "\u2603.net",
    "a\u200db\u00fc.example",
    "b\u00fc\u00adcher.de",
    "b\u00fc..example",
    "b\u00fc" + "x" * 70 + ".example",
    "M\u00dcNCHEN.de",
    "fa\u1e9e.de",
    "a\u1806b.de",
    "\u05d0\u05d1.il",
    "\u05d0a\u05d1.il",
    "a\u05d0.il",
    "xn--b\u00fc.de",
    "\u2163.example",
    "\u3371.example",
    "\U0002f868.example",
    "b\u00fc\U0001fae9.example",
    "\u0301b\u00fc.example",
    "e\u0301\u00fc.example",
    "a\u0350\u0316.de",
    "a\u0316\u0350.de",
    "\u00fca\u0301\u0316\u0350.de",
    "e\u0350\u0301.de",
    "a\u0308\u0301\u0323.de",
    "\u1100\u1161\u11a8\u00fc.kr",
    "\uac00\u11a8.kr",
    "\u0915\u093c.in",
    "\u09c7\u09be.in",
    "\U0001d15e\u00fc.example",
    "\u0f73\u00fc.example",
    "a\u00a0b\u00fc.example",
    "a\U000e0001\u00fc.example",
    "",
    "a..b",
    "a." + "b" * 64,
    "\u0130stanbul.tr",
]


def _random_idna_hosts(count: int) -> list[str]:
    """Labels mixing scripts, combining marks (some unassigned in Unicode
    3.2), jamo, compatibility and bidi characters, to pin nameprep."""
    rng = random.Random(3491)
    pools = [
        list(range(0x61, 0x7B)),
        list(range(0xC0, 0x180)),
        list(range(0x300, 0x370)),
        [0x0483, 0x0591, 0x05B0, 0x05D0, 0x05EA, 0x0627, 0x0644, 0x064B, 0x0670],
        list(range(0x1100, 0x1113)) + list(range(0x1161, 0x1176)) + [0x11A8, 0x11AF],
        [0x1E9E, 0x1806, 0x200C, 0x200D, 0x00AD, 0x2163, 0x3371, 0x2F868, 0xFB01],
        list(range(0xFF21, 0xFF3B)),
        [0x0915, 0x093C, 0x09C7, 0x09BE, 0x0F73, 0x1D15E, 0x0CC6, 0x0CC2, 0x0CD5],
        [0x1AB0, 0x1DC0, 0x1DFF, 0x20D0, 0x0350, 0x0316, 0xA66F, 0x1F600, 0x1FAE9],
    ]
    hosts = []
    for _ in range(count):
        length = rng.randint(1, 6)
        label = "".join(chr(rng.choice(rng.choice(pools))) for _ in range(length))
        hosts.append(label + ".example")
    return hosts


def build_idna() -> dict[str, Any]:
    """``host.encode("idna")``, the server name Python's ``ssl`` sends."""
    cases = []
    for host in IDNA_HOSTS + _random_idna_hosts(400):
        try:
            ascii_name: str | None = host.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_name = None
        cases.append({"host": host, "ascii": ascii_name})
    return {"cases": cases}


def build_idna_tables() -> dict[str, Any]:
    """The Unicode 3.2 stringprep tables behind Python's ``nameprep``.

    ``nyxmon_checks::pyidna`` embeds them so its IDNA encoding is Python's.
    ``unicodedata.ucd_3_2_0.normalize`` decomposes with the current tables
    except for code points unassigned in Unicode 3.2 (not decomposed) and the
    corrigenda listed in ``nfkc_3_2``; it orders and composes with the
    current combining classes (``ccc``) and compositions, which are exported
    from this Python rather than taken from a Rust crate's Unicode version.
    """
    import stringprep
    import unicodedata
    from unicodedata import ucd_3_2_0

    def ranges(predicate: Callable[[str], bool]) -> list[list[int]]:
        out: list[list[int]] = []
        for code in range(0x110000):
            if predicate(chr(code)):
                if out and out[-1][1] == code - 1:
                    out[-1][1] = code
                else:
                    out.append([code, code])
        return out

    prohibited = (
        stringprep.in_table_c12,
        stringprep.in_table_c22,
        stringprep.in_table_c3,
        stringprep.in_table_c4,
        stringprep.in_table_c5,
        stringprep.in_table_c6,
        stringprep.in_table_c7,
        stringprep.in_table_c8,
        stringprep.in_table_c9,
    )
    b2 = []
    nfkc_3_2 = []
    ccc: list[list[int]] = []
    compositions = []
    for code in range(0x110000):
        combining = unicodedata.combining(chr(code))
        if combining:
            if ccc and ccc[-1][1] == code - 1 and ccc[-1][2] == combining:
                ccc[-1][1] = code
            else:
                ccc.append([code, code, combining])
        char = chr(code)
        unassigned_now = unicodedata.category(char) == "Cn"
        unassigned_then = ucd_3_2_0.category(char) == "Cn"
        if unassigned_now and unassigned_then:
            continue
        decomposition = unicodedata.decomposition(char).split()
        if len(decomposition) == 2 and not decomposition[0].startswith("<"):
            first, second = (int(part, 16) for part in decomposition)
            if unicodedata.normalize("NFC", chr(first) + chr(second)) == char:
                compositions.append([first, second, code])
        mapped = stringprep.map_table_b2(char)
        if mapped != char:
            b2.append([code, mapped])
        if not unassigned_then:
            then = ucd_3_2_0.normalize("NFKC", char)
            if then != unicodedata.normalize("NFKC", char):
                nfkc_3_2.append([code, then])
    return {
        "b1": [code for code in range(0x110000) if stringprep.in_table_b1(chr(code))],
        "b2": b2,
        "prohibited": ranges(lambda c: any(table(c) for table in prohibited)),
        "d1": ranges(stringprep.in_table_d1),
        "d2": ranges(stringprep.in_table_d2),
        "unassigned_3_2": ranges(lambda c: ucd_3_2_0.category(c) == "Cn"),
        "nfkc_3_2": nfkc_3_2,
        "ccc": ccc,
        "compositions": compositions,
    }


def build_unicode_nonprintable() -> dict[str, Any]:
    """Code point ranges for which ``str.isprintable()`` is false.

    ``repr(str)`` escapes exactly these characters. The Rust agent embeds the
    table, so ``repr`` follows the Unicode version of the oracle Python
    instead of the Unicode version of a Rust crate.
    """
    import unicodedata

    ranges: list[list[int]] = []
    for code in range(0x110000):
        if chr(code).isprintable():
            continue
        if ranges and ranges[-1][1] == code - 1:
            ranges[-1][1] = code
        else:
            ranges.append([code, code])
    return {"unidata_version": unicodedata.unidata_version, "ranges": ranges}


# ------------------------------------------------------------------ driver


def _slice_generators(module_name: str) -> dict[str, Callable[[], Mapping[str, Any]]]:
    """``GENERATORS`` of a sibling builder module (one per slice)."""
    import importlib.util

    path = FIXTURES_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"agent_rs_{module_name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GENERATORS


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "env.json": build_env,
    "idna.json": build_idna,
    "idna_tables.json": build_idna_tables,
    "pyjson.json": build_pyjson,
    "unicode_nonprintable.json": build_unicode_nonprintable,
    **_slice_generators("gen_alerting"),
}


def render(name: str) -> str:
    """Render one fixture file exactly as it is committed."""
    content = GENERATORS[name]()
    return json.dumps(content, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def check_fixtures(directory: Path = FIXTURES_DIR) -> list[str]:
    """Return the fixture files in ``directory`` that differ from a fresh render."""
    stale = []
    for name in sorted(GENERATORS):
        path = directory / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != render(name):
            stale.append(name)
    return stale


def _ensure_import_path() -> None:
    src = FIXTURES_DIR.parent.parent / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when a committed file is stale"
    )
    args = parser.parse_args(argv)
    if os.environ.get("PYTHONHASHSEED") != "0":
        # Set reprs in Python messages depend on the hash seed; the generator
        # normalises them and also runs with a fixed seed as a safeguard.
        os.execve(
            sys.executable,
            [sys.executable, *sys.argv],
            {**os.environ, "PYTHONHASHSEED": "0"},
        )
    _ensure_import_path()
    if args.check:
        stale = check_fixtures()
        for name in stale:
            print(f"stale fixture: {name} (run `just rs-fixtures`)", file=sys.stderr)
        return 1 if stale else 0
    for name in sorted(GENERATORS):
        (FIXTURES_DIR / name).write_text(render(name), encoding="utf-8")
        print(f"wrote {FIXTURES_DIR / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
