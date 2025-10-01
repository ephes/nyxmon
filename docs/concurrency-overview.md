# Runtime Concurrency Model

NyxMon combines synchronous command handling with asynchronous I/O by composing three kinds of execution contexts: the uvloop event loop started by the CLI, background portals created from `BlockingPortalProvider`, and AnyIO worker threads. This document describes how those pieces fit together.

## 1. Startup Timeline (`entrypoints/cli.py:start_agent`)

1. **Main thread**
   - Parses CLI arguments, sets up logging, validates the SQLite path.
   - Calls `anyio.run(main, backend_options={"loop_factory": uvloop.new_event_loop})`; this starts a uvloop-based event loop on the current thread.
2. **`main()` coroutine**
   - Instantiates repositories, collector, cleaner, notifier via `bootstrap()`.
   - Enters `async with running_collector(...)` and (optionally) `running_cleaner(...)` contexts.
   - Blocks inside `await anyio.sleep_forever()` until the agent is stopped.

At this point the uvloop event loop is running on the main thread and drives every async task started by the agent (collector loops, cleaner jobs, Telegram notifier, etc.).

## 2. Portal Thread (`BlockingPortalProvider`)

Several components need to invoke async code from synchronous contexts (message bus handlers, SQLite repositories). `bootstrap()` creates a shared `BlockingPortalProvider` and injects it wherever needed. The first time a portal is requested, the provider spins up a **dedicated worker thread** hosting an AnyIO event loop. Each `with provider as portal:` block borrows that thread/loop; when it exits the portal returns to an idle pool but the thread stays alive for reuse.

Responsibilities running on the portal loop:

- `AsyncCheckRunner._async_run_all` / `_run_one`
- Async repository methods when invoked from synchronous code (`SqliteStore`)
- Notification adapters that require async tasks but expose sync hooks to the bus

## 3. Message Bus and Handlers

The service-layer message bus is synchronous. Handlers such as `handlers.execute_checks` run on whichever thread submitted the command. When a handler needs to perform async work (e.g. executing checks) it enters the portal and blocks until the async portion completes:

```
with portal_provider as portal:
    portal.call(run_checks, result_received)
```

The handler thread (often a worker created by the collector, see below) blocks during `portal.call`, but the portal’s event loop remains free to execute the async tasks.

## 4. Collector and Cleaner Threads

- **Collector (`AsyncCheckCollector`)**
  - Runs in its own daemon thread started from `collector.start()`.
  - Inside that thread it borrows the shared portal to drive the async polling loop (`_async_start`).
  - When due checks are found it dispatches `ExecuteChecks` commands by offloading `bus.handle(...)` to AnyIO’s thread pool via `anyio.to_thread.run_sync`.

- **Cleaner (`AsyncResultsCleaner`)**
  - Follows the same pattern: dedicated thread + portal + thread-pool handoff when it needs to call back into the bus.

Because the collector and cleaner trigger commands from their own threads, the message bus may service requests concurrently (subject to Python’s GIL). Each handler remains synchronous, but portal-based sections allow async work without blocking the initiating thread.

## 5. Worker Thread Pool (`anyio.to_thread.run_sync`)

Whenever async code needs to invoke a synchronous function without blocking the event loop, it calls `await anyio.to_thread.run_sync(...)`. Examples:

- `AsyncCheckRunner` delivering results via `result_received_callback`.
- Repository adapters executing SQLite operations synchronously.
- Collector dispatching `bus.handle` while its async loop continues.

AnyIO manages a shared thread pool (backed by `concurrent.futures.ThreadPoolExecutor`). Threads are created on demand and reused; they stop when the agent shuts down or become idle between tasks.

## 6. Streaming Results (`anyio.create_memory_object_stream`)

`AsyncCheckRunner` uses a memory object stream to decouple producers and consumers:

1. `_async_run_all` creates `(send_channel, receive_channel)` with `anyio.create_memory_object_stream(max_buffer_size=100)`.
2. Each `_run_one` coroutine runs concurrently in the portal loop and pushes a `Result` onto `send_channel`.
3. The outer loop inside `_async_run_all` iterates over `receive_channel`, yielding results back to `run_checks` as they arrive.
4. `run_checks` forwards each result to the synchronous callback via `to_thread.run_sync`, allowing the event loop to continue scheduling other checks while the callback persists data.

When all `_run_one` tasks finish, `_async_run_all` closes the send channel so the consumer terminates cleanly.

## 7. Concurrency Summary

| Component | Thread / Loop | Concurrency behaviour |
|-----------|---------------|-----------------------|
| CLI main (`main()` coroutine) | Main thread, uvloop | Drives long-lived async services (collector loop, cleaner loop, notifier). |
| `BlockingPortalProvider` | Dedicated portal thread | Executes bursty async work invoked from synchronous handlers (check execution, async DB ops). |
| Collector thread | Separate daemon thread | Polls for due checks; uses portal + thread pool to issue commands without blocking its loop. |
| Cleaner thread | Separate daemon thread | Same pattern as collector. |
| Thread-pool workers | Managed by AnyIO | Run synchronous callbacks (result persistence, `bus.handle`). Spawned as needed. |

## 8. Interaction Between Handlers and Async Code

1. Collector finds due checks → uses thread pool to call `bus.handle(ExecuteChecks)`.
2. `execute_checks` handler (running on that worker thread) collects the checks and invokes `runner.run_all`.
3. `run_all` enters the portal and launches `_async_run_all` on the portal loop.
4. Each `_run_one` executes the appropriate executor; when it produces a `Result` it sends it through the channel.
5. `run_checks` receives each result and hands it back to the handler via `to_thread.run_sync`. The handler thread processes the callback (stores result, schedules next run) and returns.
6. Once all results are processed, control unwinds: the portal exits, the handler finishes, and the collector loop resumes polling.

## 9. Shutdown

- Exiting the CLI loop (Ctrl+C) cancels the main uvloop tasks; context managers (`running_collector`, `running_cleaner`) signal the worker threads to stop.
- The portal is closed automatically when the provider is garbage collected or explicitly disposed.
- Any pending tasks on the portal thread are cancelled; executors must handle cancellation gracefully.

This model allows NyxMon to keep synchronous domain/state management while performing network I/O concurrently, using AnyIO primitives to cross the sync/async boundary safely.
