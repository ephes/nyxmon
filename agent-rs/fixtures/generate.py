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
import math
import os
import random
import struct
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

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

GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "pyjson.json": build_pyjson,
    "unicode_nonprintable.json": build_unicode_nonprintable,
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
