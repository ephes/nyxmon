import anyio
import aiosqlite
import pytest

from nyxmon.adapters.repositories.in_memory import InMemoryResultRepository
from nyxmon.adapters.repositories.sqlite_repo import SqliteResultRepository
from nyxmon.domain import Result


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_get_missing_and_pruned_result(backend, tmp_path):
    result = Result(result_id=1, check_id=7, status="ok", data={"sample": 42})
    if backend == "memory":
        repo = InMemoryResultRepository()
        repo.add(result)
        repo._timestamps[1] = 0
    else:
        path = tmp_path / "results.sqlite"
        repo = SqliteResultRepository(path)
        await repo._add_async(result)
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE check_result SET created_at = '2000-01-01 00:00:00'"
            )
            await db.commit()

    with pytest.raises(KeyError):
        await anyio.to_thread.run_sync(repo.get, 2)

    stored = await anyio.to_thread.run_sync(repo.get, 1)
    assert (stored.result_id, stored.check_id, stored.status, stored.data) == (
        1,
        7,
        "ok",
        {"sample": 42},
    )

    assert await repo.delete_old_results_async(retention_seconds=86400) == 1
    with pytest.raises(KeyError):
        await anyio.to_thread.run_sync(repo.get, 1)
