"""Mailbox cleanup must preserve unrelated mail and stable identities."""

from datetime import datetime, timezone

import anyio
import pytest

from nyxmon.adapters.runner.executors.imap_executor import ImapLibSession
from nyxmon.domain.imap_config import ImapCheckConfig


class Mailbox:
    def __init__(self, uidplus=True, disappear=False):
        self.uidplus = uidplus
        self.disappear = disappear
        self.messages = ["10", "20", "30"]
        self.deleted = {"10"}
        self.calls = []

    def capability(self):
        return "OK", [b"IMAP4rev1 UIDPLUS" if self.uidplus else b"IMAP4rev1"]

    # Legacy methods deliberately reject mutable sequence-number operations.
    def search(self, *args):
        raise AssertionError("sequence-number SEARCH used")

    def fetch(self, *args):
        raise AssertionError("sequence-number FETCH used")

    def store(self, *args):
        raise AssertionError("sequence-number STORE used")

    def expunge(self):
        raise AssertionError("mailbox-wide EXPUNGE used")

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "SEARCH":
            self.messages.remove("10")
            if self.disappear:
                self.messages.remove("20")
            return "OK", [b"20"]
        if command == "FETCH":
            if args[0] not in self.messages:
                return "OK", [None]
            date = datetime.now(timezone.utc).strftime("%d-%b-%Y %H:%M:%S +0000")
            return "OK", [f'1 (UID {args[0]} INTERNALDATE "{date}")'.encode()]
        if command == "STORE":
            self.deleted.update(i for i in args[0].split(",") if i in self.messages)
        if command == "EXPUNGE":
            targets = set(args[0].split(",")) & self.deleted
            self.messages = [i for i in self.messages if i not in targets]
        return "OK", [None]


def session(mailbox):
    config = ImapCheckConfig(
        host="imap.example.com",
        username="user",
        password="secret",
        search_subject="monitor",
    )
    instance = ImapLibSession(config.host, config)
    instance._conn = mailbox
    return instance


@pytest.mark.parametrize("uidplus", [True, False])
def test_cleanup_only_expunges_target_uids(uidplus):
    mailbox = Mailbox(uidplus=uidplus)
    anyio.run(session(mailbox).delete_messages, ["20"])
    assert "10" in mailbox.messages
    assert "30" in mailbox.messages
    assert ("20" not in mailbox.messages) == uidplus
    assert "20" in mailbox.deleted
    if uidplus:
        assert ("EXPUNGE", ("20",)) in mailbox.calls
    else:
        assert not any(c[0] == "EXPUNGE" for c in mailbox.calls)


def test_empty_cleanup_does_nothing():
    mailbox = Mailbox()
    anyio.run(session(mailbox).delete_messages, [])
    assert mailbox.calls == []
    assert mailbox.messages == ["10", "20", "30"]


@pytest.mark.parametrize("disappear", [True, False])
def test_search_and_cleanup_keep_uid_after_sequence_shift(disappear):
    mailbox = Mailbox(disappear=disappear)
    instance = session(mailbox)

    async def check():
        messages = await instance.search_recent("monitor", 30)
        await instance.delete_messages([m.uid for m in messages])
        return messages

    messages = anyio.run(check)
    assert [m.uid for m in messages] == ([] if disappear else ["20"])
    assert "30" in mailbox.messages
    assert "30" not in mailbox.deleted
    assert ("FETCH", ("20", "(INTERNALDATE)")) in mailbox.calls


@pytest.mark.parametrize("present", [True, False])
def test_fetch_ignores_unsolicited_other_uid(present):
    mailbox = Mailbox()
    date = datetime.now(timezone.utc).strftime("%d-%b-%Y %H:%M:%S +0000")
    responses = [f'2 (UID 30 INTERNALDATE "{date}")'.encode()]
    if present:
        responses.append(f'1 (INTERNALDATE "{date}" UID 20)'.encode())
    mailbox.uid = lambda *args: ("OK", responses)
    messages = anyio.run(session(mailbox)._fetch_internaldate, "20")
    assert (messages is not None) == present
