from typing import Self, TYPE_CHECKING

from ..domain import Command

if TYPE_CHECKING:
    from .message_bus import Message

from ..adapters.repositories import RepositoryStore, InMemoryStore


class UnitOfWork:
    def __init__(self, store: RepositoryStore = InMemoryStore()) -> None:
        self.store = store
        self._in_transaction = False
        self._new_messages: list["Message"] = []

    def __enter__(self) -> Self:
        # Start transaction if supported by the repository
        if hasattr(self.store, "connection"):
            self.store.connection.execute("BEGIN TRANSACTION")
            self._in_transaction = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()

    def commit(self):
        if self._in_transaction and hasattr(self.store, "connection"):
            self.store.connection.commit()
            self._in_transaction = False

    def rollback(self):
        if self._in_transaction and hasattr(self.store, "connection"):
            self.store.connection.rollback()
            self._in_transaction = False

    def add_command(self, command: Command) -> None:
        """Add a command to the unit of work."""
        self._new_messages.append(command)

    def collect_new_events(self):
        """Collect all new events from the store."""
        # collect events for seen checks
        while self.store.checks.seen:
            check = self.store.checks.seen.pop()
            while check.events:
                yield check.events.pop()
        # collect new messages
        while self._new_messages:
            yield self._new_messages.pop()
        # for repository in self.store.list():
        #     for aggregate in repository.list():
        #         while aggregate.events:
        #             yield aggregate.events.pop(0)
