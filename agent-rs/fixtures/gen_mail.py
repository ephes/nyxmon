"""Fixture builders for the SMTP and IMAP checks (slice S7).

Loaded by ``generate.py`` (``_slice_generators``); see its docstring. The builders
call the real Python code with the clock, the correlation token and the
Message-ID injected.
"""

from __future__ import annotations

import datetime
import io
import random
from email import generator as email_generator
from email import utils as email_utils
from collections.abc import Callable, Mapping
from typing import Any
from unittest import mock

import anyio

NOW = 1760000000  # 2025-10-09T08:53:20Z
TOKEN = "a1b2c3"

_BASE_SMTP = {
    "host": "smtp.example.com",
    "from_addr": "monitor@example.com",
    "to_addr": "alerts@example.com",
}

_VALUES: list[Any] = [
    None,
    True,
    False,
    0,
    1,
    2,
    587,
    65535,
    65536,
    -1,
    0.0,
    0.5,
    1.5,
    300,
    300.0,
    300.5,
    301,
    10**30,
    -(10**30),
    float("nan"),
    float("inf"),
    float("-inf"),
    "587",
    "",
    "   ",
    "x",
    "a\rb",
    "a\nb",
    "a\x0bb",
    "a\x0b",
    "a\x85b",
    "a ",
    [],
    {},
    ["x"],
    {"a": 1},
    "none",
    "starttls",
    "implicit",
    "STARTTLS",
    "monitor@example.com",
]

_SMTP_FIELDS = [
    "host",
    "port",
    "tls",
    "username",
    "password",
    "password_secret",
    "from_addr",
    "to_addr",
    "subject_prefix",
    "timeout",
    "retries",
    "retry_delay",
    "verify",
]

_MISSING = object()

ADDRESSES = [
    "monitor@example.com",
    "Monitor <monitor@example.com>",
    '"ops@night" <monitor@example.com>',
    '"Doe, Jane" <jane@example.com>',
    "Alerts <alerts@example.com>, b@c.de",
    "a@b.c, d@e.f, g@h.i",
    "foo bar <x@y.z>",
    "x@y.z (Comment)",
    "(lead) x@y.z",
    "<x@y.z>",
    "<@route,@other:x@y.z>",
    "group: a@b.c, d@e.f;",
    "undisclosed-recipients:;",
    "bad",
    "<>",
    "a@b@c",
    "a@[127.0.0.1]",
    "a.b.c@d.e",
    "a . b@c",
    '"quoted local"@example.com',
    "name <a@b.c",
    "a@b.c,",
    ", a@b.c",
    "a@b.c,,d@e.f",
    "a@b.c (x, y)",
    "a@b.c (unbalanced",
    "a@b.c) x",
    "a@b.c ((nested) c)",
    "\\(x@y.z",
    "a\\@b@c.d",
    "John Q. Public <jqp@example.com>",
    "nyxmon@wersdörfer.de",
    "Ünicode Name <x@y.de>",
    "ä@b.de",
    "x@y.de ",
    "  spaced@example.com  ",
    "tab\t<t@example.com>",
    "a@b.c; d@e.f",
    "a@b.c d@e.f",
    "@example.com",
    "local@",
    ".",
    "",
    " ",
    '"',
    "<",
    "a@b.c\x85",
    "\u00a0alert@b.c",
    "a@\u00a0b.c",
    "a" * 40 + "@" + "b" * 40 + ".example",
    # Production senders and recipients.
    "monitor@xn--wersdrfer-47a.de",
    "wersdoerfer.mailmon@gmail.com",
    "nyxmon-local-probe@xn--wersdrfer-47a.de",
    # Plain forms near their limits.
    '"Doe, Jane" <jane@example.com>, Ops <ops@example.com>',
    "a@b.c,d@e.f",
    "a@b.c,  d@e.f",
    "a@b.c , d@e.f",
    '"" <a@b.c>',
    '" spaced " <a@b.c>',
    '"a\\"b" <a@b.c>',
    "Name  Two <a@b.c>",
    "Name <a@b.c> ",
    "Name<a@b.c>",
    "<a@b.c>, <d@e.f>",
    "a..b@c.d",
    ".a@c.d",
    "a@c.d.",
    "a@-c.d",
    "=?utf-8?q?J=C3=BCrgen?= <j@x.example>",
    "=?utf-8?q?x?=@y.z",
    "x@y.z=?",
    "!#$%&'*+-/=?^_`{|}~@example.com",
    "ops+{tag}|x@example.com",
    "Q. Public <q@example.com>",
    "Dr. Who <who@example.com>",
    "A.B <a@b.c>",
    ". <a@b.c>",
    "a . b <a@b.c>",
    "a. <a@b.c>",
    "..x <a@b.c>",
    "Ünicode Name <a@b.c> (ops)",
    "Group: Jürgen <j@x.example>, a@b.example;",
    'Jürgen <"a<b"@x.example>',
    "a@\u00a0b.c",
    "x" * 1200 + " <a@b.c>",
    "n" * 60 + " <a@b.c>",
    "n" * 62 + " <a@b.c>",
    "a" * 62 + "@b.c",
    "a" * 63 + "@b.c",
    "a" * 64 + "@b.c",
]


def _random_addresses(count: int) -> list[str]:
    rng = random.Random(7781)
    alphabet = list('ab@.<>,;:"()[]\\ \t') + ["x", "ä", "y.z", "@q.r"]
    return [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
        for _ in range(count)
    ]


def _address_lists(count: int) -> list[str]:
    """Well-formed address lists with comments, quoted names and groups."""
    rng = random.Random(4417)
    words = [
        "a@b.example",
        "Name <x@y.example>",
        '"Doe, Jane" <jane@example.com>',
        "(lead) d@e.example",
        "g@h.example (trailing, comment)",
        "team: a@b.example, d@e.example;",
        "x@y.example (x, y)",
        '"q(uoted)" <q@r.example>',
        "x@y.example\u00a0",
        " spaced@example.com ",
    ]
    return [
        rng.choice([", ", ",", " , ", ",, "]).join(rng.sample(words, rng.randint(1, 3)))
        for _ in range(count)
    ]


_ATEXT = "abcxyzABCXYZ0189!#$%&'*+-/=?^_`{|}~"


def _plain_addresses(count: int) -> list[str]:
    """Values in (or one mutation away from) the plain forms the Rust agent
    accepts (PLAN.md 1.5 D-14); the test requires byte parity for every one
    Rust accepts."""
    rng = random.Random(9150)

    def atom() -> str:
        return "".join(rng.choice(_ATEXT) for _ in range(rng.randint(1, 6)))

    def dot_atom() -> str:
        return ".".join(atom() for _ in range(rng.randint(1, 3)))

    def mailbox() -> str:
        address = f"{dot_atom()}@{dot_atom()}"
        kind = rng.randrange(4)
        if kind == 0:
            return address
        if kind == 1:
            return f"<{address}>"
        if kind == 2:
            name = " ".join(
                rng.choice([atom(), atom(), f"{atom()}.", dot_atom()])
                for _ in range(rng.randint(1, 3))
            )
            return f"{name} <{address}>"
        qtext = [chr(c) for c in range(0x20, 0x7F) if chr(c) not in '"\\']
        name = "".join(rng.choice(qtext) for _ in range(rng.randint(1, 12)))
        return f'"{name}" <{address}>'

    mutations = [
        " ",
        "  ",
        "\t",
        ".",
        "..",
        "@",
        "<",
        ">",
        ",",
        '"',
        "(",
        "\\",
        "ä",
        "\u00a0",
        ";",
        ":",
    ]
    values = []
    for _ in range(count):
        value = rng.choice([", ", ","]).join(
            mailbox() for _ in range(rng.randint(1, 3))
        )
        if rng.random() < 0.3:
            at = rng.randint(0, len(value))
            value = value[:at] + rng.choice(mutations) + value[at:]
        values.append(value)
    return values


def build_smtp_text() -> dict[str, Any]:
    """The pure helper ``_build_subject``."""
    from nyxmon.adapters.runner.executors.smtp_executor import SmtpCheckExecutor

    executor = SmtpCheckExecutor(client=object())
    subjects = []
    for prefix in ["[nyxmon]", "  pad  ", "x", "äöü", "a\tb"]:
        for now in [NOW, 0, 951782400, 4102444799]:
            fake = mock.Mock(wraps=datetime.datetime)
            fake.now.return_value = datetime.datetime.fromtimestamp(
                now + 0.75, datetime.timezone.utc
            )
            with (
                mock.patch(
                    "nyxmon.adapters.runner.executors.smtp_executor.dt.datetime", fake
                ),
                mock.patch(
                    "nyxmon.adapters.runner.executors.smtp_executor.secrets.token_hex",
                    return_value=TOKEN,
                ),
            ):
                subject, token = executor._build_subject(prefix)
            subjects.append(
                {"prefix": prefix, "now": now, "subject": subject, "token": token}
            )
    return {"subjects": subjects}


class _CapturingClient:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def send_mail(self, config: Any, message: Any) -> Any:
        from nyxmon.adapters.runner.executors.smtp_executor import SmtpSendResponse

        self.calls.append((config, message))
        return SmtpSendResponse(code=250, message="accepted")


def _smtp_corpus() -> list[Any]:
    cases: list[Any] = []
    for field in _SMTP_FIELDS:
        for value in [_MISSING, *_VALUES]:
            data = dict(_BASE_SMTP)
            if value is _MISSING:
                data.pop(field, None)
            else:
                data[field] = value
            cases.append(data)
    for username, password, secret in [
        ("user", None, None),
        ("user", "", ""),
        ("user", "", "s"),
        ("user", "p", None),
        ("", None, None),
        (0, None, None),
        (["u"], None, "s"),
    ]:
        cases.append(
            {
                **_BASE_SMTP,
                "username": username,
                "password": password,
                "password_secret": secret,
            }
        )
    for address in (
        ADDRESSES + _address_lists(60) + _random_addresses(80) + _plain_addresses(250)
    ):
        cases.append({**_BASE_SMTP, "from_addr": address})
        cases.append({**_BASE_SMTP, "to_addr": address})
    cases.append({**_BASE_SMTP, "subject_prefix": "p" * 60})
    cases.append({**_BASE_SMTP, "subject_prefix": "ä prefix"})
    cases.append({**_BASE_SMTP, "from_addr": "x" * 80 + "@example.com"})
    cases += [[], ["host"], ["x"], "abc", "xhostx", 5, 5.5, True, False, 10**30, {}]
    return cases


def _flatten(message: Any, utf8: bool) -> dict[str, Any]:
    out = io.BytesIO()
    try:
        if utf8:
            gen = email_generator.BytesGenerator(
                out, policy=message.policy.clone(utf8=True)
            )
        else:
            gen = email_generator.BytesGenerator(out)
        gen.flatten(message, linesep="\r\n")
    except Exception as exc:  # noqa: BLE001 - recorded
        return {"error": type(exc).__name__}
    return {"bytes": out.getvalue().decode("latin-1")}


def _config_fields(config: Any) -> dict[str, Any]:
    return {
        "host": config.host,
        "port": config.port,
        "tls": config.tls,
        "username": config.username,
        "password": config.password,
        "password_secret": config.password_secret,
        "from_addr": config.from_addr,
        "to_addr": config.to_addr,
        "subject_prefix": config.subject_prefix,
        "timeout": config.timeout,
        "retries": config.retries,
        "retry_delay": config.retry_delay,
        "verify": config.verify,
        "get_password": config.get_password(),
    }


def build_config_smtp() -> dict[str, Any]:
    """``SmtpCheckConfig.from_dict`` + ``validate`` and the message build
    (the executor's steps before any I/O), with the clock, token and
    Message-ID injected."""
    from nyxmon.adapters.runner.executors import smtp_executor
    from nyxmon.domain import Check, CheckType

    results = []
    for data in _smtp_corpus():
        client = _CapturingClient()
        executor = smtp_executor.SmtpCheckExecutor(client=client)
        check = Check(
            check_id=1,
            service_id=1,
            name="smtp",
            check_type=CheckType.SMTP,
            url="smtp.example.com",
            data=data,
        )
        fake = mock.Mock(wraps=datetime.datetime)
        fake.now.return_value = datetime.datetime.fromtimestamp(
            NOW, datetime.timezone.utc
        )
        with (
            mock.patch.object(smtp_executor.dt, "datetime", fake),
            mock.patch.object(smtp_executor.secrets, "token_hex", return_value=TOKEN),
            mock.patch.object(
                smtp_executor,
                "make_msgid",
                side_effect=lambda domain=None: f"<1.2.3@{domain}>",
            ),
        ):
            try:
                result = anyio.run(executor.execute, check)
            except Exception as exc:  # noqa: BLE001 - recorded
                results.append({"data": data, "exception": type(exc).__name__})
                continue
        case: dict[str, Any] = {"data": data, "status": result.status}
        if not client.calls:
            case["result"] = result.data
            results.append(case)
            continue
        config, message = client.calls[0]
        from_value = str(message["From"])
        to_value = str(message["To"])
        envelope_from = email_utils.getaddresses([from_value])[0][1]
        recipients = [a[1] for a in email_utils.getaddresses([to_value])]
        international = not "".join([envelope_from, *recipients]).isascii()
        address_only = isinstance(data, dict) and data.keys() == _BASE_SMTP.keys()
        if not address_only:
            # Address corpus cases share the base configuration; omitted
            # to keep the fixture small.
            case["config"] = _config_fields(config)
        case.update(
            {
                "subject": str(message["Subject"]),
                "message_id": str(message["Message-ID"]),
                "envelope_from": envelope_from,
                "recipients": recipients,
                "flat": _flatten(message, international),
            }
        )
        results.append(case)
    return {"now": NOW, "token": TOKEN, "cases": results}


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "config_smtp.json": build_config_smtp,
    "smtp_text.json": build_smtp_text,
}
