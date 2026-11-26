# AsyncCheckRunner Execution Flow

This note documents how `src/nyxmon/adapters/runner/async_runner.py` bridges NyxMon's synchronous message bus with the asynchronous check executors. It complements the high-level architecture description and is intended as an implementation-level guide.

**Last Updated:** 2026-11-26

## 1. Entry Point: `CheckRunner.run_all`

1. The service-layer handler (`src/nyxmon/service_layer/handlers.py`) calls `CheckRunner.run_all(checks, result_received)` inside the synchronous message bus loop.
2. `AsyncCheckRunner` implements that interface. Its constructor receives a shared `BlockingPortalProvider` (created in `src/nyxmon/bootstrap.py`) so it can spin up an AnyIO event loop when required.

## 2. Using a Portal to Enter the Async World

`run_all` passes an async result callback to `_async_run_batch`. To execute this coroutine from synchronous code, it:

- enters the portal context (`with self.portal_provider as portal:`);
- calls `portal.call(self._async_run_batch, checks, receive)` which blocks until the coroutine finishes.

The portal ensures all asynchronous work happens on a dedicated AnyIO loop while the caller remains synchronous.

## 3. `_async_run_batch`: Fan Out Work and Gather Results

```
_async_run_batch
│
├─ create memory object stream and separate senders for results and intake stop
├─ pre-scan checks to determine required resources
│   └─ conditionally create httpx.AsyncClient (only for HTTP, JSON-HTTP or JSON metrics checks)
├─ register per-check-type executors
├─ run checks concurrently in a producer task that owns the result sender
├─ read from receive_channel concurrently and await each result callback immediately
└─ clean up executors and HTTP client (in finally block)
```

Key details:

- **Resource Scoping:** The HTTP client is created for HTTP, JSON-HTTP or JSON metrics checks. DNS-only batches avoid the overhead entirely.
- The task group stays inside one async function; no async generator yields
  across its cancel scope. The private `_async_run_all` iterator helper collects
  the full batch before yielding for callers that need an iterator.
- The service handler persists each result as it arrives, including its normal
  notification handling, so a slow sibling cannot strand a finished sample during
  graceful shutdown. A persistence or notification exception is logged per sample
  and does not abort sibling executions.
- The memory channels decouple producers (`_run_one`) from the consumer (`_async_run_batch`).
- Executors are registered per batch and can optionally receive shared resources (e.g. the HTTP client).

## 4. Executor Registry

`AsyncCheckRunner` keeps an `ExecutorRegistry` for startup validation. Each batch
constructs its own registry with its own executors, sharing the HTTP client
within that batch. Overlapping batches dispatch and close their executors
independently. The registry supports:

- **Executor Factory Pattern:** Executors can be registered as factories that create instances with per-batch context.
- **Strict Type Checking:** Missing check types raise `UnknownCheckTypeError` instead of silently falling back.
- **Cleanup Protocol:** Executors can implement `aclose()` for resource cleanup.

`_register_executors` wires up:

- `HttpCheckExecutor` for `http` and `json-http` check types:
  - Accepts optional `httpx.AsyncClient` (shared if batch contains HTTP checks)
  - Can create its own client if needed
  - Implements `aclose()` to clean up self-created clients
- `DnsCheckExecutor` for `dns` checks:
  - Performs DNS lookups via `dnspython`
  - Supports optional source-IP binding for split-horizon DNS
  - Implements `aclose()` as no-op (no resources to clean)
- `PingCheckExecutor` for `ping` checks:
  - Resolves the host once, then runs the system `ping` binary for one echo
    request per attempt via `anyio.run_process`, killing it after the
    per-attempt timeout plus a two second grace
  - Needs no privileges in the agent; reports `permission_error` when the
    binary itself lacks ICMP privileges
  - Implements `aclose()` as no-op (each process is reaped per attempt)

Additional executors can be added without modifying `_run_one`; they only need to be registered against a new check type.

## 5. `_run_one`: Execute a Single Check

Each task group member executes `_run_one(check, send_channel, registry)`:

1. Look up the executor for `check.check_type` in the registry.
2. **Raises `UnknownCheckTypeError` if no executor is registered** (no fallback).
3. Await `executor.execute(check)`.
4. On success or failure, push the resulting `Result` onto the `send_channel`.

**Breaking Change:** The legacy `_run_http_check` fallback has been removed. All check types must have registered executors.

For the retained Python HTTP executor, a successful retry-enabled check reports
`duration_ms` for the successful attempt from request start through response
stream close. It uses a monotonic clock, and retry sleeps are outside that
measurement. Result timestamps still use wall time.

If the HTTP response stream fails to close after headers arrive, the result
uses the received status and configured Location validation as usual. It
includes `cleanup_error_type: response_close_error`, the observed status code,
and the received Location when the check validates that header. Retryable
status codes still follow the configured retry policy. A close error by itself
never starts another attempt.

## 6. Returning to the Synchronous Callback

`_async_run_batch` awaits the result callback for each sample as soon as it
arrives. The callback uses `anyio.to_thread.run_sync(result_received, result)`
to invoke the synchronous service handler without blocking the event loop. That
handler schedules the next check and calls the ordinary `AddCheckResult` handler
to persist the sample and apply notification policy. A per-sample exception is
logged without aborting siblings.

## 7. Resource Cleanup and Ordering Guarantees

- The batch detaches its cached executors before attempting each distinct
  instance's close once. It reports the first close failure after attempting
  the other closes within the cleanup budget.
- The runner attempts both executor and shared HTTP client cleanup within one
  shielded one-second budget. A batch failure remains the raised exception
  with cleanup errors attached as notes. If the batch succeeds, the first
  cleanup failure is raised and any later cleanup error is attached to it.
- Task-group cancellation: if one `_run_one` raises, AnyIO cancels the others. Each executor must convert domain errors into `ResultStatus.ERROR` so failures are reported rather than propagated. Unexpected exceptions are wrapped in `BaseExceptionGroup` by AnyIO.
- The producer and intake watcher own independent send endpoints. On normal
  completion the producer cancels the idle watcher, and end of stream follows
  after both endpoints close and the queued results drain.
- Intake stop: `run_all` takes an optional `IntakeStop` (from `ExecuteChecks`).
  When the collector sets `requested` at the end of the shutdown grace period,
  the watcher cancels the running checks and retains its sender until it
  queues the marker behind completed results, even when the buffer is full
  and the producer closes its sender. The consumer handles those results, stops at the marker
  and sets `drained`, without waiting for checks whose executor thread cannot be
  interrupted.
- The shared portal remains available to the collector until shutdown; `run_all`
  returns after its batch task group and resource cleanup finish.

## 8. Opportunities for Future Refinement

- **Back-pressure tuning:** `max_buffer_size=100` is arbitrary; monitoring extremely large batches may need a different value or a semaphore
- **Batch-level metrics:** Track HTTP client reuse, executor instantiation, and cleanup timing

Understanding this flow should make it easier to extend the runner (additional check types, instrumentation, etc.) while keeping the synchronous message bus intact.
