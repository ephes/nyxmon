"""Result streaming keeps scope ownership and isolates per-sample failures."""

import threading
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from nyxmon.adapters.runner import AsyncCheckRunner
from nyxmon.adapters.repositories import InMemoryStore
from nyxmon.bootstrap import bootstrap
from nyxmon.domain import Check, CheckStatus, Result, ResultStatus
from nyxmon.domain.commands import ExecuteChecks
from nyxmon.service_layer import handlers


def check(check_id):
    return Check(
        check_id=check_id,
        service_id=1,
        name=str(check_id),
        check_type="http",
        url="https://example.test",
        data={},
    )


@pytest.mark.anyio
async def test_result_handler_failure_does_not_abort_waiting_sibling(
    monkeypatch, caplog
):
    store = InMemoryStore()
    from anyio.from_thread import BlockingPortalProvider

    runner = AsyncCheckRunner(BlockingPortalProvider())
    bus = bootstrap(store=store, runner=runner, portal_provider=runner.portal_provider)
    for check_id in (1, 2):
        store.checks.add(check(check_id))
    claims = await store.checks.list_due_checks_async()
    first_attempted = threading.Event()
    original = handlers.add_check_result

    def handle_result(command, *args):
        if command.check_result.check.check_id == 1:
            first_attempted.set()
            raise RuntimeError("injected persistence failure")
        return original(command, *args)

    async def execute(check, send_channel):
        if check.check_id == 2:
            with anyio.fail_after(2):
                while not first_attempted.is_set():
                    await anyio.sleep(0.01)
        await send_channel.send(
            Result(check_id=check.check_id, status=ResultStatus.OK, data={})
        )

    monkeypatch.setattr(handlers, "add_check_result", handle_result)
    monkeypatch.setattr(runner, "_run_one", execute)
    await anyio.to_thread.run_sync(bus.handle, ExecuteChecks(checks=claims))
    assert first_attempted.is_set()
    assert [(result.check_id, result.status) for result in store.results.list()] == [
        (2, ResultStatus.OK)
    ]
    assert store.checks.get(2).status == CheckStatus.IDLE
    assert "result handling failed for check_id=1" in caplog.text


@pytest.mark.anyio
async def test_callback_exception_cancels_siblings_and_closes_resources(monkeypatch):
    runner = AsyncCheckRunner(MagicMock())
    cleanup = AsyncMock()
    http_client = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(
        "nyxmon.adapters.runner.async_runner.httpx.AsyncClient",
        lambda **kw: http_client,
    )
    monkeypatch.setattr(runner.executor_registry, "aclose_all", cleanup)
    sibling_started = anyio.Event()
    sibling_stopped = anyio.Event()

    async def execute(check, send_channel):
        if check.check_id == 1:
            await sibling_started.wait()
            await send_channel.send(Result(check_id=1, status=ResultStatus.OK, data={}))
        else:
            sibling_started.set()
            try:
                await anyio.sleep_forever()
            finally:
                sibling_stopped.set()

    async def receive(result):
        raise RuntimeError("callback failure")

    monkeypatch.setattr(runner, "_run_one", execute)
    with pytest.raises(ExceptionGroup, match="unhandled errors") as error:
        await runner._async_run_batch([check(1), check(2)], receive)
    assert isinstance(error.value.exceptions[0], RuntimeError)
    assert sibling_stopped.is_set()
    cleanup.assert_awaited_once()
    http_client.aclose.assert_awaited_once()


@pytest.mark.anyio
async def test_cancellation_finishes_async_cleanup(monkeypatch):
    runner = AsyncCheckRunner(MagicMock())
    receiving = anyio.Event()
    cleanup_done = anyio.Event()
    http_client = MagicMock(aclose=AsyncMock())
    monkeypatch.setattr(
        "nyxmon.adapters.runner.async_runner.httpx.AsyncClient",
        lambda **kw: http_client,
    )

    async def cleanup():
        await anyio.sleep(0)
        cleanup_done.set()

    async def execute(check, send_channel):
        await send_channel.send(
            Result(check_id=check.check_id, status=ResultStatus.OK, data={})
        )

    async def receive(result):
        receiving.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(runner.executor_registry, "aclose_all", cleanup)
    monkeypatch.setattr(runner, "_run_one", execute)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(runner._async_run_batch, [check(1)], receive)
        await receiving.wait()
        tasks.cancel_scope.cancel()
    assert cleanup_done.is_set()
    http_client.aclose.assert_awaited_once()
