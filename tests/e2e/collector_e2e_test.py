import anyio
import pytest

from nyxmon.adapters.collector import running_collector, AsyncCheckCollector
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckStatus
from nyxmon.domain.commands import AddCheck
from nyxmon.adapters.repositories import SqliteStore


@pytest.fixture
def db_path(tmp_path):
    yield tmp_path / "checks.db"


@pytest.mark.anyio
async def test_agent_executes_checks(db_path):
    # async function to wait for the result
    async def wait_for_result(timeout=5.0):
        start_time = anyio.current_time()
        while anyio.current_time() - start_time < timeout:
            results = await bus.uow.store.results._list_async()
            if results:
                return results[0]
            await anyio.sleep(0.15)  # Small polling interval
        raise TimeoutError("No results found within timeout")

    # Given a bootstrapped message bus
    store = SqliteStore(db_path=db_path)
    collector = AsyncCheckCollector(interval=0.1)
    bus = bootstrap(store=store, collector=collector)

    # and a check in the repository
    check = Check(
        check_id=1,
        name="my-check",
        service_id=1,
        check_type="http",
        url="http://127.0.0.1:1337",
        data={},
    )
    cmd = AddCheck(check=check)
    bus.handle(cmd)

    # When the check collector is started
    async with running_collector(bus):
        # Then the result should be stored in the repository
        result = await wait_for_result()

        assert result.check_id == check.check_id
        assert result.data["error_msg"] == "All connection attempts failed"

        # And the check should be idle again
        [check] = await bus.uow.store.checks.list_async()
        assert check.status == CheckStatus.IDLE

        # And the next check time should be updated
        assert check.next_check_time > anyio.current_time()
