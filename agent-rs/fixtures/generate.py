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


# --------------------------------------------------------------- notifiers
# Slice S8 (PLAN.md 5.11): Telegram message, MarkdownV2 helpers, the OpsGate
# ticket payload and prompt, and whole notify calls against scripted HTTP
# responses (httpx.MockTransport), with the clock and the boundary nonce
# injected.

NOTIFIER_NONCE = "0123456789abcdef"
#: 2025-10-14T12:00:00.123456Z
NOTIFIER_NOW_MICROS = 1_760_443_200_123_456
NOTIFIER_TOKEN = "123456:SECRET-bot_token~x"
NOTIFIER_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}


def build_unicode_untrusted() -> dict[str, Any]:
    """Code point ranges ``_sanitize_untrusted_text`` replaces by ``?``.

    The categories Cc, Cf, Zl and Zp of the oracle's Unicode database, so the
    Rust sanitizer follows the oracle instead of a Rust crate's Unicode version.
    """
    import unicodedata

    ranges: list[list[int]] = []
    for code in range(0x110000):
        if unicodedata.category(chr(code)) not in NOTIFIER_CATEGORIES:
            continue
        if ranges and ranges[-1][1] == code - 1:
            ranges[-1][1] = code
        else:
            ranges.append([code, code])
    return {
        "categories": sorted(NOTIFIER_CATEGORIES),
        "unidata_version": unicodedata.unidata_version,
        "ranges": ranges,
    }


def _notifier_check(spec: Mapping[str, Any]) -> Any:
    from nyxmon.domain.models import Check

    return Check(
        check_id=spec["check_id"],
        service_id=spec["service_id"],
        name=spec["name"],
        check_type=spec["check_type"],
        url=spec["url"],
        data={},
    )


def _notifier_result(spec: Mapping[str, Any]) -> Any:
    from nyxmon.domain.models import Result

    return Result(
        check_id=spec["check_id"], status=spec["status"], data=json.loads(spec["data"])
    )


def _check_spec(
    check_id: int = 11,
    name: str = "Disk Pressure",
    check_type: str = "json-http",
    url: str = "https://example.internal/health",
    service_id: int = 22,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "service_id": service_id,
        "name": name,
        "check_type": check_type,
        "url": url,
    }


def _result_spec(
    data: Any, status: str = "error", check_id: int = 11
) -> dict[str, Any]:
    return {"check_id": check_id, "status": status, "data": json.dumps(data)}


def _injection_data() -> Any:
    path = (
        FIXTURES_DIR.parent.parent / "tests/unit/fixtures/opsgate_prompt_injection.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def _notifier_clock(now_micros: int = NOTIFIER_NOW_MICROS) -> Iterator[None]:
    from datetime import UTC, datetime, timedelta

    from nyxmon.adapters import notification

    fixed = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=now_micros)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:  # type: ignore[override]
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    with (
        mock.patch.object(notification, "datetime", _FixedDatetime),
        mock.patch.object(
            notification.secrets, "token_hex", lambda nbytes=None: NOTIFIER_NONCE
        ),
    ):
        yield


@contextmanager
def _notifier_logs() -> Iterator[list[list[str]]]:
    records: list[list[str]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append([record.levelname, record.getMessage()])

    logger = logging.getLogger("nyxmon.adapters.notification")
    handler = _Handler(level=logging.DEBUG)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


_ESCAPE_CORPUS = [
    "",
    "plain",
    "trailing\\",
    "C:\\path\\.",
    "double \\\\ backslash",
    "\\\\.",
    "mixed_[a](b)*c*~d~`e` > #+-=|{}.!",
    "_*[]()~`>#+-=|{}.!\\",
    "https://example.test/a_b?x=1&y=(2)#frag",
    "caf\u00e9 \U0001f600 \u202e\u200b",
    "line\nbreak\ttab\r",
    "Disk Pressure",
    "<b>&amp;</b> 'quotes' \"double\"",
]

_PLAIN_TEXT_CORPUS = [
    "",
    "plain",
    "*bold* _italic_ ~strike~ `code` |spoiler|",
    "\\*not bold\\*",
    "trailing\\",
    "a\\\\b",
    "[label](https://x.test/a_b\\)c)",
    "[label](unterminated",
    "]( at start",
    "a](b) c",
    "[[nested]](u)",
    "[a](b\\\\)c)",
    "x [y] z",
    "]",
    "](",
    "[",
    "\\",
    "\U0001f534 *Check Failed \\(Critical\\)*\nName: a\\_b",
    "\\[\\]\\(\\)",
    "](\\)",
    "a)b](c)d)e",
]

_TRUNCATE_CORPUS = [
    ("", 0),
    ("", 5),
    ("abc", 0),
    ("abc", 2),
    ("abc", 3),
    ("abc", 4),
    ("\U0001f600" * 5, 3),
    ("e\u0301" * 3, 4),
    ("x" * 2001, 2000),
    ("x" * 2000, 2000),
]


def _alert_data_cases() -> list[tuple[str, dict[str, Any], Any]]:
    """(label, check spec, result data) for the message builder."""
    big = 123456789012345678901234567890
    return [
        (
            "basic",
            _check_spec(),
            {"error_msg": "Disk full", "error_type": "threshold", "status_code": 500},
        ),
        ("empty data", _check_spec(), {}),
        ("empty name", _check_spec(name=""), {"error_msg": "x"}),
        (
            "status zero",
            _check_spec(),
            {"status_code": 0, "error_type": "", "error_msg": ""},
        ),
        (
            "status string",
            _check_spec(),
            {"status_code": "503", "error_type": "http_error"},
        ),
        ("status empty list", _check_spec(), {"status_code": [], "error_type": None}),
        ("status false", _check_spec(), {"status_code": False}),
        ("status true", _check_spec(), {"status_code": True}),
        ("status float", _check_spec(), {"status_code": 1e16, "error_type": 0.1}),
        (
            "status nan",
            _check_spec(),
            {"status_code": float("nan"), "error_msg": float("-inf")},
        ),
        ("status big", _check_spec(), {"status_code": big, "error_msg": -big}),
        ("negative zero", _check_spec(), {"error_msg": -0.0, "status_code": 0.0}),
        ("error_msg none", _check_spec(), {"error_msg": None, "error_type": "x"}),
        (
            "containers",
            _check_spec(),
            {
                "error_msg": {"a": [1, 2.5, None, True, "it's"], "b": {}, "c": "q\"'"},
                "error_type": ["x", {"y": 1}],
                "status_code": {"k": "v"},
            },
        ),
        ("container empty dict", _check_spec(), {"error_msg": {}, "status_code": {}}),
        (
            "repr escapes",
            _check_spec(),
            {"error_msg": ["tab\tnl\n\\ \x00 \u200b caf\u00e9 \U0001f600"]},
        ),
        (
            "markdown chars",
            _check_spec(name="db_[prod]*", url="https://x.test/a_b?q=(1)!"),
            {
                "error_msg": "C:\\path\\. failed > 5 #tag",
                "error_type": "a-b.c",
                "status_code": "4|2",
            },
        ),
        (
            "long fields",
            _check_spec(name="N" * 250, url="https://x.test/" + "u" * 600),
            {
                "error_msg": "E" * 2500,
                "error_type": "T" * 150,
                "status_code": "S" * 150,
            },
        ),
        ("long escaped", _check_spec(name="." * 201), {"error_msg": "." * 2001}),
        (
            "unicode",
            _check_spec(name="Pr\u00fcfung \U0001f600"),
            {"error_msg": "\u202edanger\u200b\n"},
        ),
        ("injection", _check_spec(), _injection_data()),
    ]


def _message_cases() -> list[dict[str, Any]]:
    created = {"status": "created", "ticket_id": "T-1_a.b"}
    tricky = {"status": "created", "ticket_id": "id)\\x"}
    cases: list[dict[str, Any]] = []
    for label, check, data in _alert_data_cases():
        for status in ("error", "warning"):
            cases.append(
                {
                    "label": f"{label} {status}",
                    "check": check,
                    "result": _result_spec(
                        data, status=status, check_id=check["check_id"]
                    ),
                    "include_warnings": False,
                    "ticket": None,
                }
            )
    base_check = _check_spec()
    base_data = {"error_msg": "Disk full", "error_type": "threshold"}
    internal = _check_spec(check_id=0, name="Collector", check_type="internal", url="")
    for ticket in (created, tricky, {"status": "duplicate"}, {"status": "error"}, None):
        for status, include in (
            ("error", False),
            ("warning", False),
            ("warning", True),
        ):
            cases.append(
                {
                    "label": f"ticket {ticket and ticket['status']} {status} include={include}",
                    "check": base_check,
                    "result": _result_spec(base_data, status=status),
                    "include_warnings": include,
                    "ticket": ticket,
                }
            )
    for check in (
        internal,
        _check_spec(check_id=0),
        _check_spec(check_type="internal"),
    ):
        for flag in (False, True, 0, None, "false"):
            cases.append(
                {
                    "label": f"opsgate_ticket={flag!r} on {check['check_id']}/{check['check_type']}",
                    "check": check,
                    "result": _result_spec(
                        {"error_msg": "x", "opsgate_ticket": flag},
                        check_id=check["check_id"],
                    ),
                    "include_warnings": True,
                    "ticket": created,
                }
            )
    return cases


def _build_message_case(case: Mapping[str, Any]) -> dict[str, Any]:
    import asyncio

    from nyxmon.adapters.notification import AsyncTelegramNotifier

    env = {
        "OPSGATE_APPROVAL_BASE_URL": "https://approve.example/base",
        "OPSGATE_SUBMIT_INCLUDE_WARNINGS": "1" if case["include_warnings"] else "",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        notifier = AsyncTelegramNotifier(token="T", chat_id="C")
    requested: list[bool] = []
    sent: dict[str, Any] = {}

    async def fake_ticket(check: Any, result: Any) -> Any:
        requested.append(True)
        return case["ticket"]

    async def fake_send(text: str, high_priority: bool = False) -> bool:
        sent.update(text=text, high_priority=high_priority)
        return True

    notifier._create_opsgate_ticket = fake_ticket  # type: ignore[method-assign]
    notifier.async_send = fake_send  # type: ignore[method-assign]
    asyncio.run(
        notifier.async_notify_check_failed(
            _notifier_check(case["check"]), _notifier_result(case["result"])
        )
    )
    return {**case, "ticket_requested": bool(requested), **sent}


def _sanitize_corpus() -> list[Any]:
    many = list(range(30))
    return [
        None,
        True,
        0,
        -1.5,
        float("nan"),
        123456789012345678901234567890,
        "",
        "plain",
        "a" * 200,
        "a" * 201,
        "\u00e9" * 300,
        "line\nbreak\r\ttab\x00\x7f\x85\u2028\u2029\u202e\u200b\ufeff\U000e0001\u00ad",
        "\ue000 \U000f0000 \u0378 \U0001f600",
        [],
        {},
        many,
        list(range(25)),
        list(range(26)),
        {str(i): i for i in range(25)},
        {str(i): i for i in range(28)},
        {"[omitted]": "mine", **{f"k{i}": i for i in range(26)}},
        {f"k{i}": i for i in range(24)} | {"[omitted]": "mine", "z": 1, "y": 2},
        {"k" * 300: "v", "k" * 301: "w"},
        {"a\nb": 1, "a?b": 2, "a\rb": 3},
        {"nested": {"a": {"b": {"c": {"d": {"e": "deep"}}}}}},
        [[[[["deep"]]]]],
        [[[["three", 1, None]]]],
        [[[[[]]]]],
        {"l": [{"x": [1, {"y": 2}]}]},
        {"f": [0.1, 1e16, -0.0, float("inf")], "b": [True, False, None]},
        _injection_data(),
        {"`backtick`": "```", "\u00e9": "\U0001f600"},
    ]


def _prompt_cases() -> list[dict[str, Any]]:
    long_url = "https://example.internal/" + "segment/" * 60 + "health"
    checks = [
        _check_spec(),
        _check_spec(name="", url=long_url),
        _check_spec(
            name="evil\nname\u202e",
            url="https://x.test/\r\npath\u200b",
            check_type="http",
        ),
        _check_spec(check_id=0, name="Collector", check_type="internal", url=""),
        _check_spec(check_id=-5, name="Pr\u00fcfung \U0001f600 `x`", service_id=0),
    ]
    datas = [
        {},
        {"error_msg": "Disk full", "error_type": "threshold", "status_code": 500},
        _injection_data(),
        {"incident_key": "  stale-batch:7  "},
        {"incident_key": "   "},
        {"incident_key": 5},
        {"incident_key": "site:connectivity", "opsgate_ticket": False},
        {"f": float("nan"), "g": float("-inf"), "big": 2**70},
        {"`": "`\u00e9\U0001f600\n"},
    ]
    cases = []
    for check in checks:
        for data in datas:
            for status in ("error", "warning"):
                cases.append(
                    {
                        "check": check,
                        "result": _result_spec(
                            data, status=status, check_id=check["check_id"]
                        ),
                    }
                )
    return cases


def _payload_case(
    case: Mapping[str, Any], expires: str | None, now_micros: int
) -> dict[str, Any]:
    import httpx

    from nyxmon.adapters.notification import AsyncTelegramNotifier

    env = {"OPSGATE_SUBMIT_BASE_URL": "https://opsgate.example"}
    if expires is not None:
        env["OPSGATE_TICKET_EXPIRES_SECONDS"] = expires
    with mock.patch.dict(os.environ, env, clear=True), _notifier_clock(now_micros):
        notifier = AsyncTelegramNotifier(token="T", chat_id="C")
        check = _notifier_check(case["check"])
        result = _notifier_result(case["result"])
        out: dict[str, Any] = {
            "check": case["check"],
            "result": case["result"],
            "expires_seconds": expires,
            "now_micros": now_micros,
            "task_ref": notifier._opsgate_task_ref(check, result),
        }
        try:
            payload = notifier._build_opsgate_ticket_payload(check, result)
        except OverflowError as exc:
            return {**out, "raised": type(exc).__name__}
    try:
        body = httpx.Request("POST", "https://opsgate.example/", json=payload).content
    except ValueError as exc:
        return {**out, "encode_error": str(exc), "expires_at": payload["expires_at"]}
    return {**out, "body": body.decode("utf-8")}


def _expiry_cases() -> list[tuple[str | None, int]]:
    from datetime import UTC, datetime

    whole = NOTIFIER_NOW_MICROS - NOTIFIER_NOW_MICROS % 1_000_000
    now_seconds = whole // 1_000_000
    last = int(datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC).timestamp())
    leap = int(datetime(2028, 2, 28, 23, 0, 0, tzinfo=UTC).timestamp()) * 1_000_000
    return [
        (None, NOTIFIER_NOW_MICROS),
        ("60", NOTIFIER_NOW_MICROS),
        ("60", whole),
        ("1", whole + 999_999),
        ("7200", leap),
        ("86400", int(datetime(2027, 12, 31, 12, tzinfo=UTC).timestamp()) * 1_000_000),
        ("100000000000", 0),
        (str(last - now_seconds), NOTIFIER_NOW_MICROS),
        (str(last - now_seconds + 1), NOTIFIER_NOW_MICROS),
        (str(999_999_999 * 86400 + 86399), NOTIFIER_NOW_MICROS),
        (str(1_000_000_000 * 86400), NOTIFIER_NOW_MICROS),
        (str(2**31 * 86400), NOTIFIER_NOW_MICROS),
        (str(10**30), NOTIFIER_NOW_MICROS),
    ]


def build_opsgate() -> dict[str, Any]:
    from nyxmon.adapters.notification import (
        AsyncTelegramNotifier,
        _sanitize_untrusted_text,
        sanitize_untrusted_data,
    )

    sanitize = [
        {
            "input": json.dumps(value),
            "output": json.dumps(sanitize_untrusted_data(value)),
        }
        for value in _sanitize_corpus()
    ]
    texts = [
        "",
        "a\x00b\x1fc\x7fd\x9fe",
        "\u2028\u2029\u202e\u200b\u200d\ufeff\u00ad\u0600\U000110bd\U000e007f",
        "x" * 199,
        "x" * 200,
        "x" * 201,
        "\U0001f600" * 250,
    ]
    sanitize_text = [
        {
            "input": text,
            "max_chars": limit,
            "output": _sanitize_untrusted_text(text, limit),
        }
        for text in texts
        for limit in (None, 200, 0, 3)
    ]
    with mock.patch.dict(os.environ, {}, clear=True):
        notifier = AsyncTelegramNotifier(token="T", chat_id="C")
    prompts = []
    with _notifier_clock():
        for case in _prompt_cases():
            check = _notifier_check(case["check"])
            result = _notifier_result(case["result"])
            prompts.append(
                {
                    **case,
                    "task_ref": notifier._opsgate_task_ref(check, result),
                    "prompt": notifier._build_opsgate_prompt(check, result),
                }
            )
    payloads = [
        _payload_case(case, "14400", NOTIFIER_NOW_MICROS)
        for case in _prompt_cases()[::4]
    ]
    base = _prompt_cases()[1]
    payloads += [_payload_case(base, expires, now) for expires, now in _expiry_cases()]
    return {
        "nonce": NOTIFIER_NONCE,
        "sanitize": sanitize,
        "sanitize_text": sanitize_text,
        "prompts": prompts,
        "payloads": payloads,
    }


def build_telegram() -> dict[str, Any]:
    from nyxmon.adapters.notification import (
        AsyncTelegramNotifier,
        _truncate,
        markdown_v2_to_plain_text,
    )

    escape = AsyncTelegramNotifier.escape_markdown_v2
    plain_inputs = _PLAIN_TEXT_CORPUS + [escape(text) for text in _ESCAPE_CORPUS]
    return {
        "now_micros": NOTIFIER_NOW_MICROS,
        "nonce": NOTIFIER_NONCE,
        "token": NOTIFIER_TOKEN,
        "escape": [{"input": t, "output": escape(t)} for t in _ESCAPE_CORPUS],
        "plain_text": [
            {"input": t, "output": markdown_v2_to_plain_text(t)} for t in plain_inputs
        ],
        "link_url": [
            {"input": t, "output": AsyncTelegramNotifier._escape_markdown_link_url(t)}
            for t in ("https://a.test/x", "https://a.test/x)y\\z", "", "))\\\\")
        ],
        "truncate": [
            {"input": value, "max_chars": limit, "output": _truncate(value, limit)}
            for value, limit in _TRUNCATE_CORPUS
        ],
        "messages": [_build_message_case(case) for case in _message_cases()],
        "http": [_run_http_scenario(spec) for spec in _http_scenarios()],
    }


_TELEGRAM_OK = {"status": 200, "body": '{"ok":true,"result":{"message_id":1}}'}
_TELEGRAM_PARSE_ERROR = {
    "status": 400,
    "body": '{"ok":false,"error_code":400,"description":"Bad Request: Can\'t parse '
    "entities: Character '.' is reserved and must be escaped with the preceding '\\\\'\"}",
}


def _http_scenarios() -> list[dict[str, Any]]:
    """Whole ``async_notify_check_failed`` calls against scripted responses.

    ``telegram`` and ``opsgate`` are the scripted responses in request order.
    The OpsGate approval base is always set explicitly, so the message text
    does not depend on where the submit endpoint lives.
    """
    telegram_env = {"TELEGRAM_BOT_TOKEN": NOTIFIER_TOKEN, "TELEGRAM_CHAT_ID": "-100123"}
    opsgate_env = {
        **telegram_env,
        "OPSGATE_SUBMIT_BASE_URL": "https://opsgate.example",
        "OPSGATE_SUBMIT_TOKEN": "opsgate-secret",
        "OPSGATE_APPROVAL_BASE_URL": "https://approve.example/",
        "OPSGATE_SUBMIT_TIMEOUT_SECONDS": "7",
    }
    basic = {"error_msg": "Disk full", "error_type": "threshold", "status_code": 500}
    check = _check_spec()
    internal = _check_spec(check_id=0, name="", check_type="internal", url="")
    token_body = "x" * 490 + NOTIFIER_TOKEN + "y" * 600

    def scenario(label: str, env: dict[str, str], **kw: Any) -> dict[str, Any]:
        return {
            "label": label,
            "env": env,
            "check": kw.get("check", check),
            "result": kw.get("result", _result_spec(basic)),
            "telegram": kw.get("telegram", []),
            "opsgate": kw.get("opsgate", []),
        }

    return [
        scenario("delivered", telegram_env, telegram=[_TELEGRAM_OK]),
        scenario(
            "warning is silent",
            telegram_env,
            result=_result_spec(basic, status="warning"),
            telegram=[{"status": 204, "body": ""}],
        ),
        scenario(
            "form encoding",
            {**telegram_env, "TELEGRAM_CHAT_ID": " -100 123 \u00e9&=+%~*"},
            check=_check_spec(name="a b+c&d=e%f~g*h/i\u00e9\U0001f600"),
            result=_result_spec({"error_msg": "x\ny\r\nz\t"}),
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "parse error resent as plain text",
            telegram_env,
            telegram=[_TELEGRAM_PARSE_ERROR, _TELEGRAM_OK],
        ),
        scenario(
            "plain text resend fails",
            telegram_env,
            telegram=[
                _TELEGRAM_PARSE_ERROR,
                {"status": 500, "body": "oops " + NOTIFIER_TOKEN},
            ],
        ),
        scenario(
            "other 400 is not resent",
            telegram_env,
            telegram=[
                {"status": 400, "body": '{"description":"Bad Request: chat not found"}'}
            ],
        ),
        scenario(
            "parse marker on a non-400 is not resent",
            telegram_env,
            telegram=[{"status": 500, "body": "can't parse entities"}],
        ),
        scenario(
            "redaction before truncation",
            telegram_env,
            telegram=[{"status": 502, "body": token_body}],
        ),
        scenario(
            "redirect is a failure",
            telegram_env,
            telegram=[
                {"status": 302, "body": "", "location": "https://elsewhere.test/"}
            ],
        ),
        scenario(
            "informational body text",
            telegram_env,
            telegram=[{"status": 403, "body": "Forbidden \u00e9 \U0001f600"}],
        ),
        scenario("missing credentials", {}),
        scenario("missing chat", {"TELEGRAM_BOT_TOKEN": NOTIFIER_TOKEN}),
        scenario("empty token", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "1"}),
        scenario(
            "ticket without telegram credentials",
            {k: v for k, v in opsgate_env.items() if not k.startswith("TELEGRAM")},
            opsgate=[{"status": 201, "body": '{"id": "T-9"}'}],
        ),
        scenario(
            "ticket created",
            opsgate_env,
            opsgate=[{"status": 201, "body": '{"id": " T-1_a.b ", "state": "open"}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket id needs link escaping",
            opsgate_env,
            opsgate=[{"status": 201, "body": '{"id": "id)\\\\x"}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket id is a number",
            opsgate_env,
            opsgate=[{"status": 201, "body": '{"id": 42}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket id is null",
            opsgate_env,
            opsgate=[{"status": 201, "body": '{"id": null}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket created without id",
            opsgate_env,
            opsgate=[{"status": 201, "body": '{"id": "  "}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket created with a non-JSON body",
            opsgate_env,
            opsgate=[{"status": 201, "body": "created"}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket created with a list body",
            opsgate_env,
            opsgate=[{"status": 201, "body": '[{"id": "x"}]'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "duplicate ticket",
            opsgate_env,
            opsgate=[{"status": 409, "body": '{"error": "duplicate_open_ticket"}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "other conflict",
            opsgate_env,
            opsgate=[
                {"status": 409, "body": '{"error": "other", "n": [1, 2.5, null, true]}'}
            ],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "server error with a list body",
            opsgate_env,
            opsgate=[{"status": 500, "body": "[1, 2]"}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket for a warning only when included",
            opsgate_env,
            result=_result_spec(basic, status="warning"),
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "ticket for an included warning",
            {**opsgate_env, "OPSGATE_SUBMIT_INCLUDE_WARNINGS": "yes"},
            result=_result_spec(basic, status="warning"),
            opsgate=[{"status": 201, "body": '{"id": "W-1"}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "resolved internal alert opens no ticket",
            {**opsgate_env, "OPSGATE_SUBMIT_INCLUDE_WARNINGS": "1"},
            check=internal,
            result=_result_spec(
                {
                    "error_msg": "resolved",
                    "incident_key": "stale-batch",
                    "opsgate_ticket": False,
                },
                check_id=0,
            ),
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "internal alert keys off its incident",
            opsgate_env,
            check=internal,
            result=_result_spec(
                {"error_msg": "wedged", "incident_key": " execution-paused "},
                check_id=0,
            ),
            opsgate=[{"status": 409, "body": '{"error": "duplicate_open_ticket"}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "hostile data cannot suppress or steer the ticket",
            opsgate_env,
            result=_result_spec(_injection_data()),
            opsgate=[{"status": 201, "body": '{"id": "H-1"}'}],
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "non-finite data fails the ticket encoding",
            opsgate_env,
            result=_result_spec({"error_msg": "x", "value": float("nan")}),
            telegram=[_TELEGRAM_OK],
        ),
        scenario(
            "expiry overflow raises",
            {**opsgate_env, "OPSGATE_TICKET_EXPIRES_SECONDS": "300000000000"},
        ),
        scenario(
            "ticket and telegram both fail",
            opsgate_env,
            opsgate=[{"status": 503, "body": '{"detail": "down"}'}],
            telegram=[_TELEGRAM_PARSE_ERROR, {"status": 429, "body": "slow down"}],
        ),
    ]


def _run_http_scenario(spec: Mapping[str, Any]) -> dict[str, Any]:
    import asyncio

    import httpx

    from nyxmon.adapters.notification import AsyncTelegramNotifier

    queues = {"telegram": list(spec["telegram"]), "opsgate": list(spec["opsgate"])}
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = "telegram" if request.url.host == "api.telegram.org" else "opsgate"
        headers = {
            name: request.headers[name]
            for name in ("accept", "authorization", "content-type", "user-agent")
            if name in request.headers
        }
        requests.append(
            {
                "kind": kind,
                "method": request.method,
                "path": request.url.raw_path.decode("ascii"),
                "headers": headers,
                "body": request.content.decode("utf-8"),
                "timeout": request.extensions.get("timeout"),
            }
        )
        reply = queues[kind].pop(0)
        headers_out = {"content-type": "application/json"}
        if "location" in reply:
            headers_out["location"] = reply["location"]
        return httpx.Response(
            reply["status"], content=reply["body"].encode("utf-8"), headers=headers_out
        )

    real_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> Any:
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    out: dict[str, Any] = dict(spec)
    with (
        mock.patch.dict(os.environ, spec["env"], clear=True),
        _notifier_clock(),
        mock.patch.object(httpx, "AsyncClient", client_factory),
        _notifier_logs() as logs,
    ):
        notifier = AsyncTelegramNotifier()
        try:
            out["delivered"] = asyncio.run(
                notifier.async_notify_check_failed(
                    _notifier_check(spec["check"]), _notifier_result(spec["result"])
                )
            )
        except Exception as exc:  # the caller's "result handling failed" path
            out["raised"] = type(exc).__name__
    assert not queues["telegram"] and not queues["opsgate"], spec["label"]
    out["requests"] = requests
    out["logs"] = logs
    return out


# ------------------------------------------------------------- tcp (S6)


def _run_async(func: Callable[[], Any]) -> Any:
    import anyio

    return anyio.run(func)


#: (check.data as JSON text, check.url) pairs for the TCP executor's pre-I/O
#: steps: ``TcpCheckConfig.from_dict`` + ``validate`` and ``_resolve_host``.
TCP_CONFIG_CORPUS: list[tuple[str, str]] = [
    ('{"port": 443}', "example.com"),
    ('{"port": 443}', ""),
    ('{"port": 443}', "https://Example.COM:8443/path"),
    ('{"port": 443}', "HTTPS://[2001:DB8::1]:443/"),
    ('{"port": 443}', "https://[fe80::1%25En0]/"),
    ('{"port": 443}', "https://[::1"),
    ('{"port": 443}', "https://::1]/"),
    ('{"port": 443}', "https://[1.2.3.4]/"),
    ('{"port": 443}', "https://[v1.x]/"),
    ('{"port": 443}', "https://[vz.x]/"),
    ('{"port": 443}', "https://a[::1]/"),
    ('{"port": 443}', "https://[::1]x/"),
    ('{"port": 443}', "https://user:pw@Host.example:99/x"),
    ('{"port": 443}', "https://@/x"),
    ('{"port": 443}', "https:///x"),
    ('{"port": 443}', "//host.example/x"),
    ('{"port": 443}', "host.example:443"),
    ('{"port": 443}', "Host.Example"),
    ('{"port": 443}', "1http://host/"),
    ('{"port": 443}', "a_b://host/"),
    ('{"port": 443}', " \t https://h\tost.example/"),
    ('{"port": 443}', "https://h\u2100st/"),
    ('{"port": 443}', "https://h\u00e9st.example/"),
    ('{"port": 443}', "https://\u2160.example/"),
    ('{"port": 443}', "https://host.example?x#y"),
    ('{"port": 443}', "https://host.example#frag/x"),
    ('{"port": 443}', "mailto:user@example.com"),
    ('{"port": 443}', "https://:80/"),
    ('{"port": 443}', "https://h\u0130st.example/"),
    ('{"port": 443}', "smtp://mail.example.com:25"),
    ('{"port": 443}', "https://\ua7cb\u00c4.example/"),
    ('{"port": 443}', "https://\u03a3\u0391\u03a3.gr/"),
    ('{"port": 443}', "https://\u03a3.\u0391\u03a3'.gr/"),
    ('{"port": 443}', "https://\u212a.example/"),
    ('{"port": 443}', "https://h\uff0fst/"),
    ('{"port": 443}', "https://h\ufe6bst/"),
    ('{"port": 443}', "https://user\uff20h@host/"),
    ('{"port": 25, "host": "mail.example.com"}', "ignored.example"),
    ('{"port": 25, "host": ""}', "fallback.example"),
    ('{"port": 25, "host": null}', "https://fallback.example/"),
    ('{"port": 25, "host": 0}', "fallback.example"),
    ('{"port": 25, "host": 5}', ""),
    ('{"port": 25, "host": true}', ""),
    ('{"port": 25, "host": 1.5}', ""),
    ('{"port": 25, "host": ["a"]}', ""),
    ('{"port": 25, "host": {"a": 1}}', ""),
    ('{"port": 25, "host": []}', "fallback.example"),
    ("{}", "example.com"),
    ('{"host": "x"}', "example.com"),
    ('{"port": true}', "example.com"),
    ('{"port": 0}', "example.com"),
    ('{"port": 65536}', "example.com"),
    ('{"port": 65535}', "example.com"),
    ('{"port": 1}', "example.com"),
    ('{"port": 443.0}', "example.com"),
    ('{"port": "443"}', "example.com"),
    ('{"port": null}', "example.com"),
    ('{"port": 100000000000000000000000}', "example.com"),
    ('{"port": 443, "retries": -1}', "example.com"),
    ('{"port": 443, "retries": 100}', "example.com"),
    ('{"port": 443, "retries": 101}', "example.com"),
    ('{"port": 443, "retries": 1.0}', "example.com"),
    ('{"port": 443, "retries": false}', "example.com"),
    ('{"port": 443, "min_cert_days": 36500}', "example.com"),
    ('{"port": 443, "min_cert_days": 36501}', "example.com"),
    ('{"port": 443, "min_cert_days": -1}', "example.com"),
    ('{"port": 443, "verify": 1}', "example.com"),
    ('{"port": 443, "verify": null}', "example.com"),
    ('{"port": 443, "verify": false}', "example.com"),
    ('{"port": 443, "check_cert_expiry": "yes"}', "example.com"),
    ('{"port": 443, "starttls_read_greeting": 0}', "example.com"),
    ('{"port": 443, "connect_timeout": 0}', "example.com"),
    ('{"port": 443, "connect_timeout": 0.0}', "example.com"),
    ('{"port": 443, "connect_timeout": -1}', "example.com"),
    ('{"port": 443, "connect_timeout": 300}', "example.com"),
    ('{"port": 443, "connect_timeout": 300.0000001}', "example.com"),
    ('{"port": 443, "connect_timeout": 0.001}', "example.com"),
    ('{"port": 443, "connect_timeout": NaN}', "example.com"),
    ('{"port": 443, "connect_timeout": Infinity}', "example.com"),
    ('{"port": 443, "connect_timeout": "10"}', "example.com"),
    ('{"port": 443, "connect_timeout": true}', "example.com"),
    ('{"port": 443, "connect_timeout": null}', "example.com"),
    ('{"port": 443, "connect_timeout": 1000000000000000000000000}', "example.com"),
    ('{"port": 443, "tls_handshake_timeout": 0}', "example.com"),
    ('{"port": 443, "tls_handshake_timeout": 2.5}', "example.com"),
    ('{"port": 443, "retry_delay": 0}', "example.com"),
    ('{"port": 443, "retry_delay": -0.0}', "example.com"),
    ('{"port": 443, "retry_delay": -1}', "example.com"),
    ('{"port": 443, "retry_delay": 301}', "example.com"),
    ('{"port": 443, "retry_delay": 2}', "example.com"),
    ('{"port": 443, "tls_mode": "implicit"}', "example.com"),
    ('{"port": 443, "tls_mode": "IMPLICIT"}', "example.com"),
    ('{"port": 443, "tls_mode": null}', "example.com"),
    ('{"port": 443, "tls_mode": 1}', "example.com"),
    ('{"port": 443, "tls_mode": ["none"]}', "example.com"),
    ('{"port": 443, "tls_mode": {"a": 1}}', "example.com"),
    ('{"port": 25, "tls_mode": "starttls"}', "example.com"),
    ('{"port": 25, "tls_mode": "starttls", "starttls_command": ""}', "example.com"),
    ('{"port": 25, "tls_mode": "starttls", "starttls_command": null}', "example.com"),
    ('{"port": 25, "tls_mode": "starttls", "starttls_command": 0}', "example.com"),
    ('{"port": 25, "tls_mode": "starttls", "starttls_command": 5}', "example.com"),
    ('{"port": 25, "tls_mode": "starttls", "starttls_command": ["x"]}', "example.com"),
    (
        '{"port": 25, "tls_mode": "starttls", "starttls_command": "", "starttls_protocol": "smtp"}',
        "example.com",
    ),
    ('{"port": 25, "tls_mode": "none", "starttls_command": ""}', "example.com"),
    (
        '{"port": 25, "tls_mode": "starttls", "starttls_protocol": "smtp"}',
        "example.com",
    ),
    (
        '{"port": 143, "tls_mode": "starttls", "starttls_protocol": "imap"}',
        "example.com",
    ),
    (
        '{"port": 4190, "tls_mode": "starttls", "starttls_protocol": "sieve"}',
        "example.com",
    ),
    (
        '{"port": 25, "tls_mode": "starttls", "starttls_protocol": "pop3"}',
        "example.com",
    ),
    (
        '{"port": 25, "tls_mode": "starttls", "starttls_protocol": "SMTP"}',
        "example.com",
    ),
    ('{"port": 25, "starttls_protocol": "pop3"}', "example.com"),
    ('{"port": 25, "starttls_protocol": ""}', "example.com"),
    ('{"port": 25, "starttls_protocol": 0}', "example.com"),
    ('{"port": 25, "starttls_protocol": false}', "example.com"),
    ('{"port": 25, "starttls_protocol": []}', "example.com"),
    ('{"port": 25, "starttls_protocol": 1}', "example.com"),
    ('{"port": 25, "starttls_protocol": ["smtp"]}', "example.com"),
    ('{"port": 25, "sni": "override.example"}', "example.com"),
    ('{"port": 25, "sni": 5}', "example.com"),
    (
        '{"port": 443, "host": "h.example", "tls_mode": "implicit", "connect_timeout": 2,'
        ' "tls_handshake_timeout": 3.5, "retries": 3, "retry_delay": 0.5,'
        ' "check_cert_expiry": true, "min_cert_days": 30, "sni": "s.example",'
        ' "starttls_command": "STARTTLS\\r\\n", "starttls_protocol": "generic",'
        ' "starttls_read_greeting": true, "verify": false}',
        "",
    ),
    ('{"port": 443, "port": 80}', "example.com"),
    ('{"verify": 1, "port": 0, "connect_timeout": 0, "tls_mode": "x"}', "example.com"),
    ('{"port": 0, "connect_timeout": 0, "tls_mode": "x"}', "example.com"),
    ('{"port": 1, "connect_timeout": 0, "tls_mode": "x"}', "example.com"),
    ('{"port": 1, "tls_mode": "x", "starttls_protocol": "x"}', "example.com"),
    ("[]", "example.com"),
    ('["port"]', "example.com"),
    ('["x", "port"]', "example.com"),
    ("[1]", "example.com"),
    ('"port"', "example.com"),
    ('"no-p-o-r-t"', "example.com"),
    ('"airport"', "example.com"),
    ("5", "example.com"),
    ("1.5", "example.com"),
    ("true", "example.com"),
    ("false", "example.com"),
]


def _tcp_pre_io(data_json: str, url: str) -> dict[str, Any]:
    """Run ``TcpCheckExecutor.execute`` up to the first network step."""
    from nyxmon.adapters.runner.executors.tcp_executor import TcpCheckExecutor
    from nyxmon.domain import Check, CheckType

    captured: dict[str, Any] = {}

    async def fake_attempt(self: Any, host: Any, config: Any) -> dict[str, Any]:
        captured["host"] = host
        captured["config"] = config
        return {}

    check = Check(
        check_id=1,
        service_id=1,
        name="tcp",
        check_type=CheckType.TCP,
        url=url,
        data=json.loads(data_json),
    )
    with mock.patch.object(TcpCheckExecutor, "_attempt_once", fake_attempt):
        try:
            result = _run_async(lambda: TcpCheckExecutor().execute(check))
        except Exception as exc:  # noqa: BLE001 - the runner's executor_exception
            return {"kind": "exception", "class": type(exc).__name__}
    if not captured:
        return {
            "kind": result.data["error_type"],
            "error_msg": result.data["error_msg"],
            "data_keys": list(result.data),
        }
    config = captured["config"]
    return {
        "kind": "ready",
        "target": captured["host"],
        "attempts": result.data["attempts"],
        "config": {
            "port": config.port,
            "host": config.host,
            "tls_mode": config.tls_mode,
            "connect_timeout": float(config.connect_timeout),
            "tls_handshake_timeout": float(config.tls_handshake_timeout),
            "retries": config.retries,
            "retry_delay": float(config.retry_delay),
            "check_cert_expiry": config.check_cert_expiry,
            "min_cert_days": config.min_cert_days,
            "sni": config.sni,
            "starttls_command": config.starttls_command,
            "starttls_protocol": config.starttls_protocol,
            "starttls_read_greeting": config.starttls_read_greeting,
            "verify": config.verify,
        },
    }


def _py_str(value: Any) -> str:
    return str(value)


def build_config_tcp() -> dict[str, Any]:
    """TCP check configuration, target host and the ``str()`` of odd hosts."""
    cases = [
        {"data_json": data_json, "url": url, "outcome": _tcp_pre_io(data_json, url)}
        for data_json, url in TCP_CONFIG_CORPUS
    ]
    # ``f"{host}"`` of the non-string hosts a config can carry (error messages
    # and the name ``anyio.connect_tcp`` resolves).
    str_values = [
        5,
        -7,
        10**30,
        True,
        False,
        1.5,
        1e16,
        1e-7,
        float("inf"),
        float("nan"),
        -0.0,
        ["a", 1, None, True, 2.5, {"k": "it's"}],
        {"a": [1, {"b": None}], "c": "q\"uo'te", "d": "\u00e9\x00\U0001f600\u200b"},
        [],
        {},
    ]
    strs = [{"value_json": json.dumps(v), "str": _py_str(v)} for v in str_values]
    # ``ipaddress.ip_address(remote_host)`` in ``anyio.connect_tcp`` for int hosts.
    import ipaddress

    ip_ints = []
    for value in [0, 5, 2**32 - 1, 2**32, 2**128 - 1, 2**128, -1, True, False]:
        try:
            literal: str | None = ipaddress.ip_address(value).compressed
        except ValueError:
            literal = None
        ip_ints.append({"value_json": json.dumps(value), "literal": literal})
    # ``anyio``'s ``idna2008_resolve``: the name ``connect_tcp`` resolves and
    # the server name ``TLSStream.wrap`` sends.
    from anyio._core._sockets import idna2008_resolve

    idna2008 = []
    contexto_hosts = [
        "l\u00b7l.\u00fc.example",
        "a\u00b7l.\u00fc.example",
        "l\u00b7.\u00fc.example",
        "\u0375\u03b1\u00fc.example",
        "\u0375a\u00fc.example",
        "\u03b1\u0375.example",
        "\u05d0\u05f3.il",
        "a\u05f3\u00fc.il",
        "\u05f4.il",
        "\u30ab\u30fb.jp",
        "\u30fb\u00fc.jp",
        "\u4e2d\u30fb.jp",
        "\u0661\u0662.eg",
        "\u0661\u06f2.eg",
        "\u06f1\u06f2.ir",
        "\u0628\u200c\u0628.ir",
        "\u0915\u094d\u200d.in",
        "a\u200db\u00fc.example",
        "B\u00fc_x.example",
        "b\u00fc.a b.example",
        "b\u00fc.-a.example",
        "b\u00fc.ab--c.example",
        "XN--BCHER-KVA.b\u00fc.example",
        "xn--.b\u00fc.example",
        "b\u00fc." + "c" * 250,
        "b\u00fc.example..",
        "\u0644\u0627\u0661.\u00fc.example",
        "\u0627\u0661a.example",
        "1.\u05d0\u05d1.example",
        "\U000105c0.example",
        "a\U000105c0\u00fc.example",
        "\ua7cb\u00fc.example",
        "\u00fc\uff0eexample\u3002com",
        "\u00fc\u3002",
        "\u00fc.",
        ".\u00fc",
        "\u00fc..",
        "\u00fc.xn--bcher-kva.example",
        "\u00fc.xn--bcher-KVA.example",
        "\u00fc.xn--zzzzzzzzz.example",
        "\u00fc.xn--a-.example",
        "\u00fc.ab_c.example",
        "\u00fc.A\u0301.example",
        "\u00fc.\u212b.example",
        "\u00fc\u00ad\u200b.example",
        "\u00df.example",
        "\u03c2\u03a3.gr",
        "\u0915\u094d\u200c\u0937.in",
        "\u0628\u200c.ir",
        "\u0644\u0661\u06f1.ir",
        "\u05d0\u0300.il",
        "\u05d0a.il",
        "\u0661\u05d0.il",
        "\u00fc" + "a" * 70 + ".example",
        "\u00fc." + "a" * 60 + "." + "b" * 60 + "." + "c" * 60 + "." + "d" * 60,
        "\u2488.example",
        "\u00fc.\u3002.example",
        "\uff21\uff22\u00fc.example",
    ]
    for host in IDNA_HOSTS + contexto_hosts + _random_idna_hosts(400):
        try:
            ascii_name: str | None = idna2008_resolve(host).decode("ascii")
        except UnicodeError:
            ascii_name = None
        idna2008.append({"host": host, "ascii": ascii_name})
    return {
        "cases": cases,
        "idna2008": idna2008,
        "ip_from_int": ip_ints,
        "str_values": strs,
    }


class _FakeSocketStream:
    """Replays scripted ``receive`` chunks and records what is sent."""

    def __init__(self, chunks: list[bytes], end: str) -> None:
        self.chunks = list(chunks)
        self.end = end
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def receive(self, max_bytes: int = 65536) -> bytes:
        import anyio

        if not self.chunks:
            if self.end == "reset":
                raise ConnectionResetError(54, "Connection reset by peer")
            raise anyio.EndOfStream
        chunk = self.chunks.pop(0)
        if len(chunk) > max_bytes:
            self.chunks.insert(0, chunk[max_bytes:])
            chunk = chunk[:max_bytes]
        return chunk


SMTP_EHLO_OK = b"250-mail.test\r\n250-PIPELINING\r\n250 STARTTLS\r\n"

#: (protocol, read_greeting, starttls_command JSON, received chunks, end).
STARTTLS_DIALOGUES: list[tuple[str, bool, str, list[bytes], str]] = [
    ("generic", False, '"STARTTLS\\r\\n"', [b"220 go ahead\r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"+OK begin\r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"OK"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"  \r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"454 TLS not available\r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"220-multi\r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"2.0.0 fine"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"* BAD nope\r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"\xff\xfe2\xc3\xa9 ok\r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"\xc2\xb2\xc2\xb2 no\r\n"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"x" * 600], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [b"\xe2\x84\xaa"], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [], "eof"),
    ("generic", False, '"STARTTLS\\r\\n"', [], "reset"),
    ("generic", False, '"STLS\\r\\n"', [b"x" * 5000], "eof"),
    ("generic", False, '"S\\u00e9\\r\\n"', [b"220 ok\r\n"], "eof"),
    ("generic", False, "5", [b"220 ok\r\n"], "eof"),
    ("generic", True, '"STARTTLS\\r\\n"', [b"220 hi\r\n", b"220 go\r\n"], "eof"),
    ("generic", True, '"STARTTLS\\r\\n"', [b"220 hi\r\n220 go\r\n"], "eof"),
    ("generic", True, '"STARTTLS\\r\\n"', [b"220 h", b"i\r\n", b"500 no\r\n"], "eof"),
    ("generic", True, '"STARTTLS\\r\\n"', [b"220 hi"], "eof"),
    ("generic", True, '"STARTTLS\\r\\n"', [b"x" * 4096], "eof"),
    ("generic", True, '"STARTTLS\\r\\n"', [b"x" * 4095 + b"\n", b"220 go\r\n"], "eof"),
    ("generic", True, '"STARTTLS\\r\\n"', [b"x" * 4096 + b"\n"], "eof"),
    ("smtp", False, '""', [b"220 mail ready\r\n", SMTP_EHLO_OK, b"220 go\r\n"], "eof"),
    (
        "smtp",
        False,
        '""',
        [
            b"220-mail.test ESMTP\r\n220 mail.test ready\r\n",
            SMTP_EHLO_OK,
            b"220 2.0.0 Ready\r\n",
        ],
        "eof",
    ),
    ("smtp", False, '""', [b"220 mail\n", b"250 ok\n", b"220 go\n"], "eof"),
    ("smtp", False, '""', [b"220 mail\r\r\n", b"250 ok\r\n", b"220 go\r\n"], "eof"),
    ("smtp", False, '""', [b"220", b"\r\n250\r\n220\r\n"], "eof"),
    (
        "smtp",
        False,
        '""',
        [b"220 mail\r\n", SMTP_EHLO_OK, b"220 go\r\n250 extra\r\n"],
        "eof",
    ),
    ("smtp", False, '""', [b"220 mail\r\n", SMTP_EHLO_OK, b"220 go\r\nX"], "eof"),
    ("smtp", False, '""', [b"554 5.3.2 service unavailable\r\n"], "eof"),
    ("smtp", False, '""', [b"220 mail\r\n", b"550 5.7.1 go away\r\n"], "eof"),
    (
        "smtp",
        False,
        '""',
        [b"220 mail\r\n", SMTP_EHLO_OK, b"454 4.7.0 TLS not available\r\n"],
        "eof",
    ),
    (
        "smtp",
        False,
        '""',
        [b"220 mail\r\n", SMTP_EHLO_OK, b"220-go\r\n220 now\r\n"],
        "eof",
    ),
    ("smtp", False, '""', [b"22 short\r\n"], "eof"),
    ("smtp", False, '""', [b"220\r\n", b"250\r\n", b"220\r\n"], "eof"),
    ("smtp", False, '""', [b"220x mail\r\n"], "eof"),
    ("smtp", False, '""', [b"2a0 mail\r\n"], "eof"),
    ("smtp", False, '""', [b"\xc2\xb2\xc2\xb2\xc2\xb2-a\r\n", b"220 b\r\n"], "eof"),
    ("smtp", False, '""', [b"\xd9\xa2\xd9\xa2\xd9\xa0 arabic\r\n"], "eof"),
    ("smtp", False, '""', [b"220 \xff\xfe mail\r\n", b"551 \xc3\xa9\r\n"], "eof"),
    ("smtp", False, '""', [b"220-mail.test partial greeting\r\n"], "eof"),
    ("smtp", False, '""', [b"220 mail\r\n"], "reset"),
    ("smtp", False, '""', [b"220 " + b"x" * 8192 + b"\r\n"], "eof"),
    ("smtp", False, '""', [b"220-" + b"y" * 1000 + b"\r\n"] * 70, "eof"),
    ("smtp", False, '""', [b"220-" + b"z" * 600 + b"\r\n", b"500 no\r\n"], "eof"),
    (
        "imap",
        False,
        '""',
        [b"* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n", b"a1 OK Begin TLS now\r\n"],
        "eof",
    ),
    (
        "imap",
        False,
        '""',
        [b"* OK ready\r\n", b"* CAPABILITY IMAP4rev1\r\na1 OK Begin TLS now\r\n"],
        "eof",
    ),
    ("imap", False, '""', [b"* ok\r\n", b"A1 ok done\r\n"], "eof"),
    ("imap", False, '""', [b"* OK[x]\r\n", b"a1 OK[y]\r\n"], "eof"),
    ("imap", False, '""', [b"* OKAY\r\n"], "eof"),
    ("imap", False, '""', [b"* PREAUTH hi\r\n"], "eof"),
    ("imap", False, '""', [b"* BYE too many connections\r\n"], "eof"),
    (
        "imap",
        False,
        '""',
        [b"* OK ready\r\n", b"a1 BAD ok, but no STARTTLS here\r\n"],
        "eof",
    ),
    (
        "imap",
        False,
        '""',
        [b"* OK ready\r\n", b"a1 NO STARTTLS unavailable\r\n"],
        "eof",
    ),
    ("imap", False, '""', [b"* OK ready\r\n", b"* BYE shutting down\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"* bye[x]\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"a2 OK wrong tag\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"a1 OKAY\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"a1 OK\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"a1 o\xe2\x84\xaa\r\n"], "eof"),
    ("imap", False, '""', [b"* O\xe2\x84\xaa ready\r\n", b"a1 OK\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"*  odd\r\n", b"a1 OK\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"*\r\n"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n", b"a1 OK go\r\n\x16\x03"], "eof"),
    ("imap", False, '""', [b"* OK ready\r\n"], "eof"),
    (
        "sieve",
        False,
        '""',
        [
            b'"IMPLEMENTATION" "Pigeonhole"\r\n"STARTTLS"\r\nOK "ready."\r\n',
            b'OK "Begin TLS."\r\n',
        ],
        "eof",
    ),
    ("sieve", False, '""', [b"ok\r\n", b"Ok (x)\r\n"], "eof"),
    ("sieve", False, '""', [b"OK(x)\r\n", b"OK\r\n"], "eof"),
    ("sieve", False, '""', [b"OKAY\r\n", b"BYE now\r\n"], "eof"),
    ("sieve", False, '""', [b'BYE "too many connections"\r\n'], "eof"),
    ("sieve", False, '""', [b"OK ready\r\n", b'NO "TLS unavailable"\r\n'], "eof"),
    ("sieve", False, '""', [b"OK ready\r\n", b"NOPE\r\n", b"no\r\n"], "eof"),
    ("sieve", False, '""', [b"OK ready\r\n", b"BYE\r\n"], "eof"),
    ("sieve", False, '""', [b"OK ready\r\n"], "eof"),
]


def _dialogue_case(
    protocol: str, read_greeting: bool, command_json: str, chunks: list[bytes], end: str
) -> dict[str, Any]:
    from nyxmon.adapters.runner.executors.tcp_executor import (
        TcpCheckError,
        TcpCheckExecutor,
    )
    from nyxmon.domain.tcp_config import TcpCheckConfig

    config = TcpCheckConfig(
        port=1,
        tls_mode="starttls",
        starttls_protocol=protocol,
        starttls_command=json.loads(command_json),
        starttls_read_greeting=read_greeting,
    )
    stream = _FakeSocketStream(chunks, end)
    outcome: dict[str, Any]
    try:
        _run_async(
            lambda: TcpCheckExecutor()._send_starttls_command(stream, config)  # type: ignore[arg-type]
        )
        outcome = {"kind": "ok"}
    except TcpCheckError as exc:
        outcome = {
            "kind": "error",
            "error_type": exc.error_type,
            "message": exc.message,
            "retryable": exc.retryable,
            "data": exc.data,
        }
    except Exception as exc:  # noqa: BLE001 - becomes unexpected_error
        outcome = {"kind": "exception", **_error(exc)}
    return {
        "protocol": protocol,
        "read_greeting": read_greeting,
        "command_json": command_json,
        "chunks": [chunk.hex() for chunk in chunks],
        "end": end,
        "sent": [data.hex() for data in stream.sent],
        "outcome": outcome,
    }


def build_starttls_text() -> dict[str, Any]:
    """STARTTLS classification helpers, patterns and scripted dialogues."""
    from nyxmon.adapters.runner.executors import tcp_executor as tcp

    executor = tcp.TcpCheckExecutor()
    responses = [
        "",
        "220 ready",
        "220",
        "2",
        "250-ok",
        "454 no",
        "554 5.3.2 no",
        "+OK begin",
        "OK",
        "ok",
        "Okay then",
        "* BAD book",
        "BAD",
        "x220 ok",
        "2.0.0 ready",
        "go ahead",
        "\u00b2\u00b2\u00b2 superscript",
        "\u0662\u0662\u0660 arabic",
        "\u0662abc",
        "O\u212a",
        "\u212a",
        "\u0130 2",
        " 220",
        "220  x",
        "abc 220",
        "\u00b9",
    ]
    positive = [
        {"text": text, "positive": executor._is_positive_starttls_response(text)}
        for text in responses
    ]
    truncate = [
        {"text": text, "truncated": tcp._truncate(text)}
        for text in ["", "a" * 500, "a" * 501, "\u00e9" * 499 + "\U0001f600" * 3]
    ]
    pattern_lines = [
        "* OK",
        "* OK ready",
        "* OK[CAPABILITY]",
        "* ok",
        "* OKAY",
        "* OK\r",
        "* O\u212a",
        "*  OK",
        " * OK",
        "* BYE",
        "* bye now",
        "* BYE[x]",
        "* BYEBYE",
        "a1 OK",
        "A1 ok done",
        "a1 NO x",
        "a1 BAD[y]",
        "a1 OKAY",
        "a1  OK",
        "a1 o\u212a",
        "a2 OK",
        "OK",
        "OK (x)",
        "OK(x)",
        "OK ready",
        "Ok",
        "NO",
        "no thanks",
        "BYE",
        "BYEBYE",
        "OKAY",
        "",
        "\u017f",
    ]
    patterns: dict[str, Any] = {}
    for name, regex in [
        ("imap_ok_greeting", tcp._IMAP_OK_GREETING),
        ("imap_untagged_bye", tcp._IMAP_UNTAGGED_BYE),
        ("imap_tagged", tcp._IMAP_TAGGED),
        ("sieve_response", tcp._SIEVE_RESPONSE),
    ]:
        rows = []
        for line in pattern_lines:
            match = regex.match(line)
            rows.append(
                {
                    "line": line,
                    "match": match is not None,
                    "group": match.group(1) if match and regex.groups else None,
                }
            )
        patterns[name] = rows
    dialogues = [_dialogue_case(*case) for case in STARTTLS_DIALOGUES]
    return {
        "positive": positive,
        "truncate": truncate,
        "patterns": patterns,
        "dialogues": dialogues,
    }


def build_idna2008_tables() -> dict[str, Any]:
    """The data behind ``idna.encode(host, uts46=True)``.

    ``nyxmon_checks::idna2008`` embeds it so the encoding follows the ``idna``
    package's own tables (UTS 46 mapping, IDNA 2008 code point classes,
    joining types, scripts) and the oracle's ``unicodedata`` (bidirectional
    classes, combining marks, virama combining class) rather than a Rust
    crate's Unicode version.
    """
    import unicodedata

    import idna
    from idna import idnadata
    from idna.uts46data import uts46data

    def ranges(encoded: tuple[int, ...]) -> list[list[int]]:
        return [[value >> 32, (value & 0xFFFFFFFF) - 1] for value in encoded]

    bidi: list[list[Any]] = []
    for code in range(0x110000):
        direction = unicodedata.bidirectional(chr(code))
        if bidi and bidi[-1][2] == direction and bidi[-1][1] == code - 1:
            bidi[-1][1] = code
        else:
            bidi.append([code, code, direction])
    return {
        "idna_version": idna.__version__,
        "idna_unicode_version": idnadata.__version__,
        "unidata_version": unicodedata.unidata_version,
        "uts46": [list(row) + ([None] if len(row) == 2 else []) for row in uts46data],
        "classes": {
            name: ranges(values) for name, values in idnadata.codepoint_classes.items()
        },
        "scripts": {name: ranges(values) for name, values in idnadata.scripts.items()},
        "joining_types": sorted(
            [code, chr(kind)] for code, kind in idnadata.joining_types().items()
        ),
        "bidi": bidi,
        "marks": _code_ranges(lambda c: unicodedata.category(c).startswith("M")),
        "virama": _code_ranges(lambda c: unicodedata.combining(c) == 9),
    }


def _code_ranges(predicate: Callable[[str], bool]) -> list[list[int]]:
    """``[start, end]`` ranges of the code points for which ``predicate`` holds."""
    ranges: list[list[int]] = []
    for code in range(0x110000):
        if not predicate(chr(code)):
            continue
        if ranges and ranges[-1][1] == code - 1:
            ranges[-1][1] = code
        else:
            ranges.append([code, code])
    return ranges


def build_unicode_props() -> dict[str, Any]:
    """``str`` properties of the oracle's Unicode database.

    ``nyxmon_core`` embeds them so ``str.isdigit()``, ``str.lower()`` and
    ``urlsplit``'s NFKC netloc check follow CPython rather than the Unicode
    version of the Rust standard library or a crate:

    - ``digits``: ``str.isdigit()``;
    - ``lower``: ``c.lower()`` where it differs from ``c`` (U+03A3 is
      context-dependent and handled with ``cased``/``case_ignorable``);
    - ``cased``/``case_ignorable``: the properties CPython's final-sigma rule
      reads, derived from ``str.lower()`` itself;
    - ``nfkc_url_delimiters``: code points whose NFKC form contains one of
      ``/?#@:`` (``urllib.parse._checknetloc``).
    """
    import unicodedata

    def final_sigma_after(prefix: str) -> bool:
        return (prefix + "\u03a3").lower()[-1] == "\u03c2"

    lower = [
        [code, chr(code).lower()]
        for code in range(0x110000)
        if code != 0x3A3 and chr(code).lower() != chr(code)
    ]
    case_ignorable = _code_ranges(
        lambda c: final_sigma_after("a" + c) and not final_sigma_after("1" + c)
    )
    cased = _code_ranges(lambda c: final_sigma_after("1" + c))
    delimiters = [
        code
        for code in range(0x110000)
        if chr(code) not in "/?#@:"
        and any(d in unicodedata.normalize("NFKC", chr(code)) for d in "/?#@:")
    ]
    return {
        "unidata_version": unicodedata.unidata_version,
        "digits": _code_ranges(str.isdigit),
        "lower": lower,
        "cased": cased,
        "case_ignorable": case_ignorable,
        "nfkc_url_delimiters": delimiters,
    }


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
    "config_tcp.json": build_config_tcp,
    "env.json": build_env,
    "idna.json": build_idna,
    "idna2008_tables.json": build_idna2008_tables,
    "idna_tables.json": build_idna_tables,
    "opsgate.json": build_opsgate,
    "pyjson.json": build_pyjson,
    "telegram.json": build_telegram,
    "starttls_text.json": build_starttls_text,
    "unicode_nonprintable.json": build_unicode_nonprintable,
    **_slice_generators("gen_alerting"),
    **_slice_generators("gen_site"),
    **_slice_generators("gen_http"),
    "unicode_untrusted.json": build_unicode_untrusted,
    "unicode_props.json": build_unicode_props,
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
        (FIXTURES_DIR / name).parent.mkdir(parents=True, exist_ok=True)
        (FIXTURES_DIR / name).write_text(render(name), encoding="utf-8")
        print(f"wrote {FIXTURES_DIR / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
