"""Configuration admission must precede all mailbox side effects."""

from datetime import datetime, timezone

import anyio
import pytest

from nyxmon.adapters.runner.executors.imap_executor import (
    ImapCheckExecutor,
    ImapMessage,
)
from nyxmon.domain import Check, ResultStatus
from nyxmon.domain.imap_config import ImapCheckConfig

BASE = dict(
    host="imap.example.com", username="user", password="secret", search_subject="probe"
)
INVALID_CLEANUP = ["false", "true", 0, 1, None, [], {}]
INVALID_NUMERIC = [
    (field, value)
    for field in ("port", "retries", "max_age_minutes", "timeout", "retry_delay")
    for value in (
        float("nan"),
        float("inf"),
        -float("inf"),
        "2",
        [],
        {},
        None,
        True,
        False,
        10**1000,
    )
] + [
    ("port", 0),
    ("port", 65536),
    ("port", 993.0),
    ("retries", -1),
    ("retries", 1.0),
    ("retries", 101),
    ("max_age_minutes", 0),
    ("max_age_minutes", 1.0),
    ("max_age_minutes", 525601),
    ("timeout", 0),
    ("timeout", 301),
    ("retry_delay", -1),
    ("retry_delay", 301),
]


@pytest.mark.parametrize("invalid", INVALID_CLEANUP)
def test_cleanup_requires_boolean(invalid):
    assert_rejected("delete_after_check", invalid)


@pytest.mark.parametrize("field,invalid", INVALID_NUMERIC)
def test_numeric_configuration_is_rejected(field, invalid):
    assert_rejected(field, invalid)


def assert_rejected(field, invalid):
    data = {**BASE, field: invalid}
    with pytest.raises(ValueError, match=field):
        ImapCheckConfig.from_dict(data)
    config = ImapCheckConfig(**BASE)
    setattr(config, field, invalid)
    with pytest.raises(ValueError, match=field):
        config.validate()

    session = RecordingSession()
    created = []

    def factory(*_):
        created.append(True)
        return session

    result = anyio.run(ImapCheckExecutor(factory).execute, build_check(data))
    assert result.status == ResultStatus.ERROR
    assert result.data["error_type"] == "configuration_error"
    assert created == []
    assert session.deleted == []


def build_check(data):
    return Check(
        check_id=1,
        service_id=1,
        name="probe",
        check_type="imap",
        url=BASE["host"],
        data=data,
    )


class RecordingSession:
    def __init__(self):
        self.deleted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def search_recent(self, *_):
        return [
            ImapMessage(
                uid="7",
                subject="probe",
                internaldate=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]

    async def delete_messages(self, ids):
        self.deleted.extend(ids)


@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize(
    "numbers",
    [
        dict(port=1, retries=0, max_age_minutes=1, timeout=0.001, retry_delay=0),
        dict(
            port=65535,
            retries=100,
            max_age_minutes=525600,
            timeout=300,
            retry_delay=300,
        ),
    ],
)
def test_valid_boundaries_roundtrip_and_execute(cleanup, numbers):
    data = {**BASE, **numbers, "delete_after_check": cleanup}
    config = ImapCheckConfig.from_dict(data)
    assert config.validate()
    assert ImapCheckConfig.from_dict(config.to_dict()) == config
    session = RecordingSession()
    result = anyio.run(ImapCheckExecutor(lambda *_: session).execute, build_check(data))
    assert result.status == ResultStatus.OK
    assert session.deleted == (["7"] if cleanup else [])
