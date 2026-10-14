"""Fixture builders for the SMTP and IMAP checks (slice S7).

Loaded by ``generate.py`` (``_slice_generators``); see its docstring. The builders
call the real Python code with the clock, the correlation token and the
Message-ID injected.
"""

from __future__ import annotations

import datetime
import io
import random
import re
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


# ------------------------------------------------------------------ IMAP

_BASE_IMAP = {
    "host": "imap.example.com",
    "username": "monitor",
    "password": "secret",
    "search_subject": "[nyxmon]",
}

_IMAP_FIELDS = [
    "host",
    "username",
    "password",
    "password_secret",
    "search_subject",
    "folder",
    "port",
    "tls_mode",
    "max_age_minutes",
    "delete_after_check",
    "timeout",
    "retries",
    "retry_delay",
    "no_recent_message_severity",
    "verify",
]

_IMAP_EXTRA_VALUES: list[Any] = [
    100,
    101,
    525600,
    525601,
    993,
    993.0,
    "critical",
    "warning",
    "Warning",
]

_SET_REPR = re.compile(r"\{('[^']*'(?:, '[^']*')*)\}")


def _sorted_set_repr(message: str) -> str:
    """Python set reprs depend on the hash seed (PLAN.md D-2): sort them."""

    def fix(match: re.Match[str]) -> str:
        items = sorted(match.group(1).split(", "))
        return "{" + ", ".join(items) + "}"

    return _SET_REPR.sub(fix, message)


def _imap_corpus() -> list[Any]:
    cases: list[Any] = []
    for field in _IMAP_FIELDS:
        for value in [_MISSING, *_VALUES, *_IMAP_EXTRA_VALUES]:
            data = dict(_BASE_IMAP)
            if value is _MISSING:
                data.pop(field, None)
            else:
                data[field] = value
            cases.append(data)
    cases.append({**_BASE_IMAP, "password": None, "password_secret": "s"})
    cases.append({**_BASE_IMAP, "password": "", "password_secret": ""})
    cases.append({**_BASE_IMAP, "password": 0, "password_secret": ["s"]})
    cases += [[], ["host"], "abc", "xhostx", 5, 5.5, True, 10**30, {}]
    return cases


def build_config_imap() -> dict[str, Any]:
    """``ImapCheckConfig.from_dict`` (which validates) and ``validate`` as the
    executor runs them: a ``ValueError`` is a ``configuration_error``,
    anything else escapes the executor."""
    from nyxmon.domain.imap_config import ImapCheckConfig

    results = []
    for data in _imap_corpus():
        case: dict[str, Any] = {"data": data}
        try:
            config = ImapCheckConfig.from_dict(data)
            config.validate()
        except Exception as exc:  # noqa: BLE001 - recorded
            case["error"] = {
                "class": type(exc).__name__,
                "message": _sorted_set_repr(str(exc)),
            }
        else:
            case["config"] = config.to_dict()
        results.append(case)
    return {"cases": results}


IMAP_HOSTS = [
    "imap.example.com",
    "IMAP.Example.COM",
    "imaps://Mail.Example.com:993",
    "imap://user:pw@mail.example.com/INBOX",
    "imap://[::1]:993",
    "imap://[FE80::1%eth0]/",
    "imap://[127.0.0.1]",
    "imap://[::1",
    "imap://::1]",
    "imap://x[::1]",
    "imap://",
    "imap:///path",
    "1imap://host",
    "  imap://host",
    "imap://ho\tst",
    "x://host?q#f",
    "://host",
    "imap://host:abc",
    "imap://@host",
    "a://b://c",
]

INTERNALDATES = [
    b'1 (UID 20 INTERNALDATE "14-Oct-2026 12:00:00 +0200")',
    b'1 (INTERNALDATE "14-Oct-2026 12:00:00 -0130" UID 20)',
    b'1 (UID 20 INTERNALDATE " 4-Oct-2026 01:02:03 +0000")',
    b'1 (UID 20 INTERNALDATE "29-Feb-2024 23:59:59 -1200")',
    b'1 (UID 20 INTERNALDATE "01-Jan-1970 00:00:00 +0000")',
    b'1 (UID 20 INTERNALDATE "31-Dec-1969 23:59:59 +0000")',
    b'1 (UID 20 INTERNALDATE "39-Feb-2026 99:99:99 +9999")',
    b'1 (UID 20 INTERNALDATE "00-Jan-2026 01:02:03 +0000")',
    b'1 (UID 20 INTERNALDATE "04-oct-2026 01:02:03 +0000")',
    b'1 (UID 20 INTERNALDATE "04-Foo-2026 01:02:03 +0000")',
    b'1 (UID 20 INTERNALDATE "04-Foo-2026 01:02:03 +0000") \xff',
    b'1 (UID 20 INTERNALDATE "14-Oct-2026 12:00:00 +0200)',
    b'1 (UID 20 INTERNALDATE "14-Oct-2026 12:00 +0200")',
    b'1 (UID 20 INTERNALDATE "bad" INTERNALDATE "14-Oct-2026 12:00:00 +0000")',
    b'1 (UID 20 INTERNALDATE "14-Oct-2026 12:00:00 +0000" INTERNALDATE "bad")',
    (
        b'1 (UID 20 INTERNALDATE "14-Oct-2026 12:00:00 +0000" '
        b'INTERNALDATE "15-Oct-2026 12:00:00 +0000")'
    ),
    b"1 (UID 20 FLAGS (\\Seen))",
    b'1 (uid 20 INTERNALDATE "14-Oct-2026 12:00:00 +0000")',
    b'1 (UID 200 INTERNALDATE "14-Oct-2026 12:00:00 +0000")',
    b'1 (UID 20x INTERNALDATE "14-Oct-2026 12:00:00 +0000")',
    b'1 (XUID 20 UID 20 INTERNALDATE "14-Oct-2026 12:00:00 +0000")',
    b'1 (UID 21 UID 20 INTERNALDATE "14-Oct-2026 12:00:00 +0000")',
    b'1 (UID 20 INTERNALDATE "14-Oct-2026 12:00:00 +0000") \xff',
]

SEARCH_DATA = [
    b"",
    b"20",
    b"20 30  40",
    b" 7\t8 ",
    b"20\x1c30",
    b"\xc3\xa4 5",
    b"5 \xff",
    b"5 \xe2\x82",
    b"5 \xe2\x28\xa1",
    b"5 \xc0\xaf",
    b"5 \xed\xa0\x80",
    b"5 \xf4\x90\x80\x80",
    b"5 \xf0\x9f\x98",
]


def build_imap_text() -> dict[str, Any]:
    """``_normalize_host``, the UID match of ``_fetch_internaldate``, the
    INTERNALDATE conversion and the SEARCH result split."""
    from nyxmon.adapters.runner.executors.imap_executor import (
        ImapCheckExecutor,
        ImapLibSession,
    )
    from nyxmon.domain.imap_config import ImapCheckConfig

    executor = ImapCheckExecutor()
    hosts = []
    for host in IMAP_HOSTS:
        try:
            hosts.append({"host": host, "normalized": executor._normalize_host(host)})
        except Exception as exc:  # noqa: BLE001 - recorded
            hosts.append({"host": host, "error": str(exc)})
    config = ImapCheckConfig.from_dict(_BASE_IMAP)

    def session_with(*responses: list[Any]) -> tuple[ImapLibSession, list[Any]]:
        session = ImapLibSession("imap.example.com", config)
        session._conn = object()  # type: ignore[assignment]
        queue = list(responses)
        calls: list[Any] = []

        async def checked(*args: Any) -> list[Any]:
            calls.append(args)
            return queue.pop(0)

        session._run_checked = checked  # type: ignore[method-assign]
        return session, calls

    dates = []
    for line in INTERNALDATES:
        case: dict[str, Any] = {"line": line.decode("latin-1")}
        try:
            value = anyio.run(session_with([line])[0]._fetch_internaldate, "20")
        except Exception as exc:  # noqa: BLE001 - recorded
            case["error"] = str(exc)
        else:
            case["iso"] = None if value is None else value.isoformat()
        dates.append(case)
    searches = []
    for data in SEARCH_DATA:
        case = {"data": data.decode("latin-1")}
        session, calls = session_with([data], *([[None]] * 8))
        try:
            anyio.run(session.search_recent, "[nyxmon]", 30)
        except Exception as exc:  # noqa: BLE001 - recorded
            case["error"] = str(exc)
        else:
            # Each id is fetched once (``[None]``: no INTERNALDATE).
            case["ids"] = [call[2] for call in calls[1:]]
        searches.append(case)
    return {"hosts": hosts, "internaldates": dates, "searches": searches}


# IMAP executor dialogues: the real ``ImapCheckExecutor`` (``imaplib``) talks
# to a scripted server on 127.0.0.1. A script is a list of steps:
# ["send", text] writes text, ["recv"] reads and records one client line,
# ["tagged", text] answers "<tag of the last received line> <text>\r\n",
# ["close"] closes the connection. After the last step the server records
# whatever the client still sends until it closes. Recorded lines drop the
# tag (imaplib's tag prefix is random).

_IMAP_NOW = NOW + 0.75
_RECENT = "09-Oct-2025 08:50:00 +0000"
_RECENT_EARLIER = "09-Oct-2025 10:45:00 +0200"
_OLD = "09-Oct-2025 07:00:00 +0000"
_GREETING = "* OK [CAPABILITY IMAP4rev1 UIDPLUS] ready\r\n"


_DOVECOT_CAPS = (
    "IMAP4rev1 SASL-IR LOGIN-REFERRALS ID ENABLE IDLE SORT SORT=DISPLAY THREAD=REFERENCES "
    "THREAD=REFS THREAD=ORDEREDSUBJECT MULTIAPPEND URL-PARTIAL CATENATE UNSELECT CHILDREN "
    "NAMESPACE UIDPLUS LIST-EXTENDED I18NLEVEL=1 CONDSTORE QRESYNC ESEARCH ESORT SEARCHRES "
    "WITHIN CONTEXT=SEARCH LIST-STATUS BINARY MOVE SNIPPET=FUZZY PREVIEW=FUZZY PREVIEW "
    "STATUS=SIZE SAVEDATE LITERAL+ NOTIFY SPECIAL-USE"
)
_DOVECOT_GREETING = (
    "* OK [CAPABILITY IMAP4rev1 SASL-IR LOGIN-REFERRALS ID ENABLE IDLE LITERAL+ "
    "STARTTLS AUTH=PLAIN] Dovecot (Ubuntu) ready.\r\n"
)
_DOVECOT_SELECT = (
    "* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)\r\n"
    "* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft \\*)] "
    "Flags permitted.\r\n"
    "* 2 EXISTS\r\n* 0 RECENT\r\n"
    "* OK [UIDVALIDITY 1700000000] UIDs valid\r\n"
    "* OK [UIDNEXT 13] Predicted next UID\r\n"
    "* OK [HIGHESTMODSEQ 40] Highest\r\n"
)
_GMAIL_PRE_CAPS = (
    "IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 XYZZY SASL-IR "
    "AUTH=XOAUTH2 AUTH=PLAIN AUTH=PLAIN-CLIENTTOKEN AUTH=OAUTHBEARER"
)
_GMAIL_CAPS = (
    "IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 UIDPLUS "
    "COMPRESS=DEFLATE ENABLE MOVE CONDSTORE ESEARCH UTF8=ACCEPT LIST-EXTENDED LIST-STATUS "
    "LITERAL- SPECIAL-USE APPENDLIMIT=35651584"
)
_GMAIL_SELECT = (
    "* FLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen $NotPhishing $Phishing)\r\n"
    "* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen $NotPhishing "
    "$Phishing \\*)] Flags permitted.\r\n"
    "* OK [UIDVALIDITY 3] UIDs valid.\r\n"
    "* 5 EXISTS\r\n* 0 RECENT\r\n"
    "* OK [UIDNEXT 4712] Predicted next UID.\r\n"
    "* OK [HIGHESTMODSEQ 987654]\r\n"
)


def _untag(line: str) -> str:
    return line.split(" ", 1)[1] if " " in line else line


def _serve_imap(listener: Any, scripts: list[list[list[str]]], out: list[Any]) -> None:
    for script in scripts:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        conn.settimeout(10)
        reader = conn.makefile("rb")
        seen: list[str] = []
        tag = ""

        def read_line(reader: Any = reader) -> str | None:
            try:
                raw = reader.readline()
            except OSError:
                return None
            if not raw:
                return None
            return raw.rstrip(b"\r\n").decode("latin-1")

        closed = False
        for step in script:
            if step[0] == "send":
                conn.sendall(step[1].encode("latin-1"))
            elif step[0] == "tagged":
                conn.sendall(f"{tag} {step[1]}\r\n".encode("latin-1"))
            elif step[0] == "recv":
                line = read_line()
                if line is None:
                    closed = True
                    break
                tag = line.split(" ", 1)[0]
                seen.append(_untag(line))
            elif step[0] == "close":
                closed = True
                break
        if not closed:
            while (line := read_line()) is not None:
                seen.append(_untag(line))
        reader.close()
        conn.close()
        out.append(seen)


def _login_select() -> list[list[str]]:
    return [
        ["send", _GREETING],
        ["recv"],
        ["tagged", "OK [CAPABILITY IMAP4rev1 UIDPLUS] Logged in"],
        ["recv"],
        ["send", "* 3 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n"],
        ["tagged", "OK [READ-WRITE] SELECT completed"],
    ]


def _search(ids: str) -> list[list[str]]:
    return [
        ["recv"],
        ["send", f"* SEARCH {ids}\r\n"],
        ["tagged", "OK SEARCH completed"],
    ]


def _fetch(*responses: str) -> list[list[str]]:
    steps = [["recv"]]
    steps += [["send", f"{r}\r\n"] for r in responses]
    steps.append(["tagged", "OK FETCH completed"])
    return steps


def _store_capability(capabilities: str) -> list[list[str]]:
    return [
        ["recv"],
        ["send", "* 1 FETCH (FLAGS (\\Deleted))\r\n"],
        ["tagged", "OK STORE completed"],
        ["recv"],
        ["send", f"* CAPABILITY {capabilities}\r\n"],
        ["tagged", "OK CAPABILITY completed"],
    ]


def _logout() -> list[list[str]]:
    return [
        ["recv"],
        ["send", "* BYE logging out\r\n"],
        ["tagged", "OK LOGOUT completed"],
    ]


def _one_match(uid: str = "5") -> list[list[str]]:
    return [
        *_login_select(),
        *_search(uid),
        *_fetch(f'* 1 FETCH (UID {uid} INTERNALDATE "{_RECENT}")'),
    ]


def _imap_dialogues() -> list[tuple[str, dict[str, Any], list[list[list[str]]]]]:
    """``(name, config overrides, one script per connection)``."""
    full = [
        *_login_select(),
        *_search("20 30 40"),
        *_fetch(
            f'* 9 FETCH (UID 30 INTERNALDATE "{_RECENT_EARLIER}")',
            f'* 1 FETCH (UID 20 INTERNALDATE "{_RECENT}")',
        ),
        *_fetch(f'* 2 FETCH (INTERNALDATE "{_RECENT_EARLIER}" UID 30)'),
        *_fetch(f'* 3 FETCH (UID 40 INTERNALDATE "{_OLD}")'),
        *_store_capability("IMAP4rev1 UIDPLUS"),
        ["recv"],
        ["tagged", "OK EXPUNGE completed"],
        *_logout(),
    ]
    no_recent = [*_login_select(), *_search(""), *_logout()]
    cases: list[tuple[str, dict[str, Any], list[list[list[str]]]]] = [
        ("sorted_deleted_and_expunged_with_uidplus", {}, [full]),
        (
            "flagged_only_without_uidplus",
            {"folder": "Monitoring Box"},
            [
                [
                    ["send", "* OK ready\r\n"],
                    ["recv"],
                    ["send", "* CAPABILITY IMAP4REV1\r\n"],
                    ["tagged", "OK done"],
                    ["recv"],
                    ["tagged", "OK Logged in"],
                    ["recv"],
                    ["send", "* CAPABILITY IMAP4rev1 IDLE\r\n"],
                    ["tagged", "OK done"],
                    ["recv"],
                    ["tagged", "OK [READ-WRITE] selected"],
                    *_search("5"),
                    *_fetch(f'* 1 FETCH (UID 5 INTERNALDATE "{_RECENT}")'),
                    *_store_capability("IMAP4rev1 IDLE"),
                    *_logout(),
                ]
            ],
        ),
        (
            "kept_without_delete_after_check",
            {"delete_after_check": False},
            [[*_one_match(), *_logout()]],
        ),
        (
            "unsolicited_fetch_of_another_uid_is_ignored",
            {},
            [
                [
                    *_login_select(),
                    *_search("5"),
                    *_fetch(
                        f'* 9 FETCH (UID 6 INTERNALDATE "{_RECENT}")',
                        "* 2 FETCH (FLAGS (\\Seen))",
                    ),
                    *_logout(),
                ]
            ],
        ),
        (
            "literal_in_fetch_response",
            {},
            [
                [
                    *_login_select(),
                    *_search("7"),
                    *_fetch(
                        "* 1 FETCH (UID 7 BODY[HEADER.FIELDS (SUBJECT)] {12}\r\n"
                        "Subject: x\r\n)",
                        f'* 1 FETCH (UID 7 INTERNALDATE "{_RECENT}")',
                    ),
                    *_store_capability("IMAP4rev1"),
                    *_logout(),
                ]
            ],
        ),
        (
            "missing_internaldate_is_skipped",
            {},
            [
                [
                    *_login_select(),
                    *_search("5"),
                    *_fetch("* 1 FETCH (UID 5 FLAGS (\\Seen))"),
                    *_logout(),
                ]
            ],
        ),
        (
            "no_recent_retried_then_warning",
            {
                "retries": 1,
                "no_recent_message_severity": "warning",
                "max_age_minutes": 5,
            },
            [no_recent, no_recent],
        ),
        (
            "no_recent_then_found",
            {"retries": 2, "retry_delay": 2.5},
            [
                no_recent,
                [
                    *_one_match(),
                    *_store_capability("IMAP4rev1 UIDPLUS"),
                    ["recv"],
                    ["tagged", "OK done"],
                    *_logout(),
                ],
            ],
        ),
        (
            "old_message_only_is_critical",
            {},
            [
                [
                    *_login_select(),
                    *_search("40"),
                    *_fetch(f'* 3 FETCH (UID 40 INTERNALDATE "{_OLD}")'),
                    *_logout(),
                ]
            ],
        ),
        (
            "rejected_login",
            {"retries": 2},
            [
                [
                    ["send", _GREETING],
                    ["recv"],
                    ["tagged", "NO [AUTHENTICATIONFAILED] Authentication failed."],
                ]
            ],
        ),
        (
            "rejected_login_non_utf8",
            {},
            [[["send", _GREETING], ["recv"], ["tagged", "NO bad \xff login"]]],
        ),
        (
            "rejected_select",
            {"retries": 2},
            [
                [
                    ["send", _GREETING],
                    ["recv"],
                    ["tagged", "OK [CAPABILITY IMAP4rev1] Logged in"],
                    ["recv"],
                    ["tagged", "NO [NONEXISTENT] Mailbox doesn't exist"],
                ]
            ],
        ),
        (
            "read_only_select",
            {"folder": "Shared"},
            [
                [
                    ["send", _GREETING],
                    ["recv"],
                    ["tagged", "OK [CAPABILITY IMAP4rev1] Logged in"],
                    ["recv"],
                    ["tagged", "OK [READ-ONLY] SELECT completed"],
                ]
            ],
        ),
        (
            "search_no",
            {},
            [
                [
                    *_login_select(),
                    ["recv"],
                    ["tagged", "NO [SERVERBUG] search failed"],
                    *_logout(),
                ]
            ],
        ),
        (
            "search_bad",
            {},
            [
                [
                    *_login_select(),
                    ["recv"],
                    ["tagged", "BAD Could not parse command"],
                    *_logout(),
                ]
            ],
        ),
        (
            "fetch_no",
            {},
            [
                [
                    *_login_select(),
                    *_search("5"),
                    ["recv"],
                    ["tagged", "NO fetch failed"],
                    *_logout(),
                ]
            ],
        ),
        (
            "store_no",
            {},
            [
                [
                    *_one_match(),
                    ["recv"],
                    ["tagged", "NO [CANNOT] store failed"],
                    *_logout(),
                ]
            ],
        ),
        (
            "expunge_no",
            {},
            [
                [
                    *_one_match(),
                    *_store_capability("IMAP4rev1 UIDPLUS"),
                    ["recv"],
                    ["tagged", "NO expunge failed"],
                    *_logout(),
                ]
            ],
        ),
        (
            "connection_drops_mid_command",
            {},
            [[*_login_select(), ["recv"], ["close"]]],
        ),
        (
            "bye_during_command",
            {},
            [
                [
                    *_login_select(),
                    ["recv"],
                    ["send", "* BYE server shutting down\r\n"],
                    ["close"],
                ]
            ],
        ),
        ("bye_greeting", {}, [[["send", "* BYE go away\r\n"]]]),
        ("continuation_greeting", {}, [[["send", "+ hello\r\n"]]]),
        ("closed_before_greeting", {}, [[["close"]]]),
        ("unterminated_greeting", {}, [[["send", "* OK ready"], ["close"]]]),
        (
            "not_imap4",
            {},
            [[["send", "* OK [CAPABILITY IMAP2] ready\r\n"]]],
        ),
        (
            "no_capability_response",
            {},
            [[["send", "* OK ready\r\n"], ["recv"], ["tagged", "OK done"]]],
        ),
        (
            "preauth_greeting",
            {},
            [
                [
                    ["send", "* PREAUTH [CAPABILITY IMAP4rev1] welcome\r\n"],
                    ["recv"],
                    ["send", "* CAPABILITY IMAP4rev1\r\n"],
                    ["tagged", "OK done"],
                ]
            ],
        ),
        (
            "unexpected_response",
            {"timeout": 0.5},
            [[*_login_select(), ["recv"], ["send", "garbage\r\n"]]],
        ),
        (
            "password_secret_only",
            {"password": None, "password_secret": "s"},
            [[["send", _GREETING]]],
        ),
        (
            "quoted_credentials",
            {"username": "user name", "password": 'p"w\\x'},
            [[["send", _GREETING], ["recv"], ["tagged", "NO nope"]]],
        ),
        (
            "non_ascii_password",
            {"password": "pässword"},
            [[["send", _GREETING]]],
        ),
        (
            "non_ascii_subject",
            {"search_subject": "Prüfung"},
            [[*_login_select(), *_logout()]],
        ),
        (
            "subject_with_quote",
            {"search_subject": 'say "hi"'},
            [
                [
                    *_login_select(),
                    ["recv"],
                    ["tagged", "BAD invalid search"],
                    *_logout(),
                ]
            ],
        ),
        (
            "starttls_not_advertised",
            {"tls_mode": "starttls"},
            [[["send", "* OK [CAPABILITY IMAP4rev1] ready\r\n"]]],
        ),
        (
            "starttls_refused",
            {"tls_mode": "starttls", "retries": 2},
            [
                [
                    ["send", "* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n"],
                    ["recv"],
                    ["tagged", "NO not now"],
                ]
            ],
        ),
        (
            "starttls_bad",
            {"tls_mode": "starttls"},
            [
                [
                    ["send", "* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n"],
                    ["recv"],
                    ["tagged", "BAD no"],
                ]
            ],
        ),
        (
            "search_with_literal_data",
            {},
            [
                [
                    *_login_select(),
                    ["recv"],
                    ["send", "* SEARCH {2}\r\n20\r\n"],
                    ["tagged", "OK done"],
                    *_logout(),
                ]
            ],
        ),
        (
            "blank_lines_are_skipped",
            {"delete_after_check": False},
            [
                [
                    *_login_select(),
                    ["recv"],
                    ["send", "\r\n* SEARCH 5\r\n\r\n"],
                    ["tagged", "OK done"],
                    *_fetch(f'* 1 FETCH (UID 5 INTERNALDATE "{_RECENT}")'),
                    *_logout(),
                ]
            ],
        ),
        (
            "logout_failure_is_ignored",
            {"delete_after_check": False},
            [[*_one_match(), ["recv"], ["close"]]],
        ),
        (
            "capability_after_store_without_response",
            {},
            [
                [
                    *_one_match(),
                    ["recv"],
                    ["tagged", "OK stored"],
                    ["recv"],
                    ["tagged", "OK no caps"],
                    *_logout(),
                ]
            ],
        ),
        (
            "dovecot_like_session",
            {"tls_mode": "none", "max_age_minutes": 15},
            [
                [
                    ["send", _DOVECOT_GREETING],
                    ["recv"],
                    ["tagged", f"OK [CAPABILITY {_DOVECOT_CAPS}] Logged in"],
                    ["recv"],
                    ["send", _DOVECOT_SELECT],
                    [
                        "tagged",
                        "OK [READ-WRITE] Select completed (0.001 + 0.000 secs).",
                    ],
                    ["recv"],
                    ["send", "* SEARCH 11 12\r\n"],
                    ["tagged", "OK Search completed (0.001 + 0.000 secs)."],
                    ["recv"],
                    [
                        "send",
                        f'* 1 FETCH (UID 11 INTERNALDATE "{_RECENT_EARLIER}")\r\n',
                    ],
                    ["tagged", "OK Fetch completed (0.001 + 0.000 secs)."],
                    ["recv"],
                    ["send", f'* 2 FETCH (UID 12 INTERNALDATE "{_RECENT}")\r\n'],
                    ["tagged", "OK Fetch completed (0.001 + 0.000 secs)."],
                    ["recv"],
                    ["send", "* 1 FETCH (UID 11 FLAGS (\\Deleted))\r\n"],
                    ["send", "* 2 FETCH (UID 12 FLAGS (\\Deleted))\r\n"],
                    ["tagged", "OK Store completed (0.001 + 0.000 secs)."],
                    ["recv"],
                    ["send", f"* CAPABILITY {_DOVECOT_CAPS}\r\n"],
                    ["tagged", "OK Capability completed (0.001 + 0.000 secs)."],
                    ["recv"],
                    ["send", "* 1 EXPUNGE\r\n* 1 EXPUNGE\r\n"],
                    ["tagged", "OK Expunge completed (0.001 + 0.000 secs)."],
                    ["recv"],
                    ["send", "* BYE Logging out\r\n"],
                    ["tagged", "OK Logout completed (0.001 + 0.000 secs)."],
                ]
            ],
        ),
        (
            "gmail_like_session",
            {"username": "someone@gmail.com", "delete_after_check": True},
            [
                [
                    ["send", "* OK Gimap ready for requests from 192.0.2.1 x1mb\r\n"],
                    ["recv"],
                    ["send", f"* CAPABILITY {_GMAIL_PRE_CAPS}\r\n"],
                    ["tagged", "OK Thats all she wrote! x1mb"],
                    ["recv"],
                    ["send", f"* CAPABILITY {_GMAIL_CAPS}\r\n"],
                    ["tagged", "OK someone@gmail.com authenticated (Success)"],
                    ["recv"],
                    ["send", _GMAIL_SELECT],
                    ["tagged", "OK [READ-WRITE] INBOX selected. (Success)"],
                    ["recv"],
                    ["send", "* SEARCH 4711\r\n"],
                    ["tagged", "OK SEARCH completed (Success)"],
                    ["recv"],
                    ["send", f'* 5 FETCH (UID 4711 INTERNALDATE "{_RECENT}")\r\n'],
                    ["tagged", "OK Success"],
                    ["recv"],
                    ["send", "* 5 FETCH (UID 4711 FLAGS (\\Deleted))\r\n"],
                    ["tagged", "OK Success"],
                    ["recv"],
                    ["send", f"* CAPABILITY {_GMAIL_CAPS}\r\n"],
                    ["tagged", "OK Success"],
                    ["recv"],
                    ["send", "* 5 EXPUNGE\r\n* 4 EXISTS\r\n"],
                    ["tagged", "OK Success"],
                    ["recv"],
                    ["send", "* BYE LOGOUT Requested\r\n"],
                    ["tagged", "OK 73 good day (Success)"],
                ]
            ],
        ),
        (
            "gmail_like_login_failure",
            {"username": "someone@gmail.com"},
            [
                [
                    ["send", "* OK Gimap ready for requests from 192.0.2.1 x1mb\r\n"],
                    ["recv"],
                    ["send", f"* CAPABILITY {_GMAIL_PRE_CAPS}\r\n"],
                    ["tagged", "OK Thats all she wrote! x1mb"],
                    ["recv"],
                    [
                        "tagged",
                        "NO [AUTHENTICATIONFAILED] Invalid credentials (Failure)",
                    ],
                ]
            ],
        ),
        (
            "logout_without_bye",
            {"delete_after_check": False},
            [[*_one_match(), ["recv"], ["tagged", "OK bye"]]],
        ),
        (
            "read_only_during_session",
            {},
            [
                [
                    *_login_select(),
                    ["recv"],
                    [
                        "send",
                        "* OK [READ-ONLY] mailbox now read-only\r\n* SEARCH 5\r\n",
                    ],
                    ["tagged", "OK SEARCH completed"],
                    *_logout(),
                ]
            ],
        ),
        (
            "untagged_no_and_alert_are_ignored",
            {"delete_after_check": False},
            [
                [
                    *_login_select(),
                    ["recv"],
                    ["send", "* NO [ALERT] quota almost full\r\n* SEARCH 5\r\n"],
                    ["tagged", "OK SEARCH completed"],
                    *_fetch(f'* 1 FETCH (UID 5 INTERNALDATE "{_RECENT}")'),
                    *_logout(),
                ]
            ],
        ),
    ]
    return cases


def _run_imap_dialogue(
    overrides: dict[str, Any], scripts: list[list[list[str]]]
) -> dict[str, Any]:
    import gc
    import socket
    import threading

    from nyxmon.adapters.runner.executors import imap_executor
    from nyxmon.domain import Check

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(10)
    port = listener.getsockname()[1]
    transcripts: list[Any] = []
    server = threading.Thread(
        target=_serve_imap, args=(listener, scripts, transcripts), daemon=True
    )
    server.start()
    data = {
        **_BASE_IMAP,
        "host": "127.0.0.1",
        "port": port,
        "tls_mode": "none",
        "retries": 0,
        "retry_delay": 7,
        "timeout": 5,
        **overrides,
    }
    check = Check(
        check_id=9,
        service_id=1,
        name="imap",
        check_type="imap",
        url="imap.example.com",
        data=data,
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime.datetime.fromtimestamp(_IMAP_NOW, tz)

    with (
        mock.patch.object(imap_executor, "datetime", FixedDatetime),
        mock.patch.object(imap_executor.anyio, "sleep", fake_sleep),
    ):
        result = anyio.run(imap_executor.ImapCheckExecutor().execute, check)
    gc.collect()
    server.join(15)
    listener.close()
    assert not server.is_alive(), "scripted IMAP server did not finish"
    del data["port"]
    return {
        "data": data,
        "status": result.status,
        "result": result.data,
        "sleeps": sleeps,
        "transcripts": transcripts,
    }


def build_imap_dialogues() -> dict[str, Any]:
    """The IMAP executor (real ``imaplib``) against scripted servers."""
    cases = []
    for name, overrides, scripts in _imap_dialogues():
        case = {"name": name, "scripts": scripts}
        case.update(_run_imap_dialogue(overrides, scripts))
        cases.append(case)
    return {"now": _IMAP_NOW, "cases": cases}


GENERATORS: dict[str, Callable[[], Mapping[str, Any]]] = {
    "config_smtp.json": build_config_smtp,
    "smtp_text.json": build_smtp_text,
    "config_imap.json": build_config_imap,
    "imap_text.json": build_imap_text,
    "imap_dialogues.json": build_imap_dialogues,
}
