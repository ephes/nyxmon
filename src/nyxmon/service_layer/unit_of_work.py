from typing import Self

from ..adapters.repositories import RepositoryStore, InMemoryStore


class UnitOfWork:
    def __init__(self, store: RepositoryStore = InMemoryStore()) -> None:
        self.store = store

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args):
        self.rollback()

    def commit(self):
        pass

    def rollback(self):
        pass

    def collect_new_events(self):
        for repository in self.store.list():
            for aggregate in repository.list():
                while aggregate.events:
                    yield aggregate.events.pop(0)
