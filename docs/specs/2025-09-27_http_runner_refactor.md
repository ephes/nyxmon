§§
# AsyncCheckRunner HTTP Path Refactor

## Requirements

1. **Eliminate legacy fallback**: Remove `AsyncCheckRunner._run_http_check` so all HTTP traffic flows through `HttpCheckExecutor` and the registry. Any missing check type must surface as an explicit error instead of silently using the fallback.
2. **Resource scoping**: Avoid instantiating `httpx.AsyncClient` when a batch contains no HTTP/JSON-HTTP checks. DNS-only runs must not open unused HTTP clients.
3. **Maintain concurrency semantics**: Preserve existing behaviour where each check runs concurrently inside the AnyIO task group and results are streamed back as they arrive.
4. **Backwards compatibility**: Keep public interfaces unchanged (`CheckRunner.run_all`, executor protocol). Tests and bootstrap code should continue to work without caller changes.
5. **Instrumentation hooks**: Expose clear extension points so future executors can declare their own resource requirements without editing `_async_run_all`.

## Concept

- **Pre-scan checks**: Before entering the task group, analyse the batch to determine which check types are present. Use this to decide which executors to register and which shared resources need to be created.
- **Executor-owned resources**: Shift the HTTP client lifecycle into `HttpCheckExecutor` (e.g. accept an optional externally supplied client or lazily create its own). DNS executor already manages itself and requires no shared setup.
- **Registry initialisation**: Move one-time registration into `AsyncCheckRunner.__init__`, allowing executors to be registered with lightweight factories that can receive per-batch context if necessary.
- **Strict check type enforcement**: Replace the fallback with an explicit `UnknownCheckTypeError`, ensuring missing registrations are caught during development/testing.

## Implementation Plan

1. **Introduce executor factories**
   - Create a lightweight wrapper class or callable (e.g. `ExecutorFactory`) that can produce executor instances given an optional batch context.
   - Update `ExecutorRegistry` to store factories instead of concrete executors and to memoise per-batch instances.

2. **Refactor HTTP executor lifecycle**
   - Allow `HttpCheckExecutor` to accept an optional `httpx.AsyncClient`. If none is provided, it should lazily create one (with the current config) and close it after execution.
   - Ensure reuse within a batch by keeping the client on the executor instance and closing it when `_async_run_all` finishes.

3. **Scan checks and build batch context**
   - Inside `_async_run_all`, analyse the incoming `checks` to compute a set of check types and construct a `BatchContext` (e.g. a simple dict) with shared resources.
   - For HTTP/JSON-HTTP types, create a shared `httpx.AsyncClient` and store it in the context; skip this step for DNS-only batches.

4. **Instantiate executors per batch**
   - Teach `ExecutorRegistry` to return a per-batch executor instance (via the stored factory) when `_run_one` asks for a given type.
   - If no factory exists for a check type, raise a clear error instead of falling back.

5. **Remove legacy `_run_http_check`**
   - Delete the method and all call sites.
   - Update `_run_one` to rely solely on registered executors.

6. **Handle executor cleanup**
   - Define an optional `aclose()` coroutine on executors that manage resources (HTTP client).
   - Track instantiated executors inside `_async_run_all` and, in a `finally` block, `await` their cleanup if available.

7. **Adjust tests**
   - Update unit tests for the registry and runners to cover factory semantics and the missing-type error path.
   - Add coverage proving DNS-only batches skip HTTP client creation (e.g. spy on `httpx.AsyncClient`).
   - Ensure e2e tests still pass after removing the fallback.

8. **Documentation**
   - Refresh `docs/async-check-runner.md` to describe the new lifecycle and stricter executor enforcement.
   - Note the change in the new refactor document for future contributors.

9. **Validation**
   - Run `just test` (and targeted e2e tests involving HTTP/DNS).
   - Perform a manual smoke test with mixed HTTP/DNS checks to confirm resource usage and behaviour.

Following this plan will make the HTTP path explicit, simplify the runner’s control flow, and avoid spinning up unused HTTP clients.
