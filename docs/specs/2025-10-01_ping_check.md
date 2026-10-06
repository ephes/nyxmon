# Specification: ICMP Ping Check Implementation

**Date:** 2025-10-01
**Status:** Implemented (2026-10-06, see section 0 for deviations)
**Author:** System

## 0. Implementation Notes (2026-10-06)

The executor lives in `src/nyxmon/adapters/runner/executors/ping_executor.py`
with its configuration in `src/nyxmon/domain/ping_config.py`, and NyxBoard has
a dedicated `PingHealthCheckForm`. It deviates from the draft below in these
deliberate ways:

- **System `ping` instead of `icmplib` (option B, not A).** The agent never
  opens ICMP sockets, so it needs no `CAP_NET_RAW`, setuid or root; it relies
  on the platform `ping` binary, which already carries that privilege on
  default installs. FR6 holds: a binary without privilege yields
  `error_type="permission_error"` with a CAP_NET_RAW / `ping_group_range` /
  setuid hint. NFR2's "no new socket per check" does not apply; each attempt
  is one short-lived `ping` process for one echo request.
- **Per-attempt processes.** Each of the `count` attempts runs `ping` for a
  single echo request with the platform wait flag and is killed after
  `timeout` + 2 s, so FR2's per-attempt timeout is enforced even if the binary
  ignores its wait flag. Attempts are separated by `interval`.
- **Result field names** follow the other executors: failures carry
  `error_type`/`error_msg` instead of a bare `error`. Success data matches
  section 3.2 and adds `host` and an `attempts` list.
- **Partial loss is `ok`** (FR1/FR3); no warning threshold yet.
- **IPv6** works when the name resolves to an IPv6 address first or an IPv6
  literal is configured (macOS/BSD use `ping6`). Windows support (command
  building and localized reply parsing via the `TTL=` field or, for IPv6, the reply
  source address) is covered by
  unit tests only. OpenBSD/NetBSD get no wait flag and rely on the process
  timeout.
- **Tests** are unit tests with an injected process runner and resolver
  (`tests/unit/test_ping_executor.py`, including registration and startup
  validation); no test sends ICMP.

## 1. Overview

Implement ICMP ping checks to verify host reachability in NyxMon. This will allow monitoring of network devices and services that may not expose HTTP endpoints.

## 2. Requirements

### 2.1 Functional Requirements

**FR1: ICMP Echo Request/Reply**
- The ping executor MUST send ICMP Echo Request packets to the target host
- The ping executor MUST wait for ICMP Echo Reply packets
- The check MUST succeed if at least one reply is received within the timeout

**FR2: Timeout Handling**
- The executor MUST support configurable timeout values (default: 5 seconds)
- If no reply is received within the timeout, the check MUST fail with status ERROR
- The timeout MUST be enforced per ping attempt, not for the entire check

**FR3: Multiple Attempts**
- The executor SHOULD support multiple ping attempts (default: 3)
- The check MUST succeed if at least one attempt receives a reply
- All attempts MUST be included in the result data for monitoring purposes

**FR4: Host Resolution**
- The executor MUST support both IP addresses and hostnames
- Hostnames MUST be resolved to IP addresses before sending ICMP packets
- DNS resolution failures MUST result in check ERROR status

**FR5: Result Data**
- The result MUST include:
  - Response time (RTT) in milliseconds for successful pings
  - Number of packets sent and received
  - Packet loss percentage
  - Target IP address (resolved from hostname if applicable)
  - Error message for failed checks

**FR6: Privilege Requirements**
- The implementation MUST handle ICMP socket permission requirements gracefully
- If ICMP sockets cannot be created due to permissions, the check MUST fail with a clear error message
- The error message MUST indicate the need for appropriate privileges (CAP_NET_RAW or setuid)

### 2.2 Non-Functional Requirements

**NFR1: Platform Support**
- The implementation MUST work on Linux, macOS, and Windows
- Platform-specific behavior MUST be documented

**NFR2: Performance**
- Ping checks MUST run asynchronously without blocking other checks
- The executor MUST reuse network resources efficiently
- The implementation MUST NOT create new sockets for each check

**NFR3: Error Handling**
- Network errors MUST be caught and converted to ERROR status
- Permission errors MUST be clearly distinguished from network failures
- All errors MUST include actionable error messages

**NFR4: Testing**
- Unit tests MUST cover successful pings, timeouts, and DNS resolution
- Integration tests MUST verify executor registration and lifecycle
- Tests MUST NOT require root privileges or special capabilities

## 3. Design

### 3.1 Check Configuration

Ping checks will use the existing `Check` model with the following data structure:

```python
{
    "check_id": 123,
    "check_type": "ping",
    "name": "Gateway Ping",
    "url": "192.168.1.1",  # or hostname like "gateway.local"
    "data": {
        "timeout": 5,      # seconds, optional, default: 5
        "count": 3,        # attempts, optional, default: 3
        "interval": 1      # seconds between attempts, optional, default: 1
    }
}
```

### 3.2 Result Structure

Successful ping result:
```python
{
    "status": "ok",
    "data": {
        "target": "192.168.1.1",
        "hostname": "gateway.local",  # if resolved
        "packets_sent": 3,
        "packets_received": 3,
        "packet_loss_percent": 0.0,
        "rtt_min_ms": 0.234,
        "rtt_max_ms": 1.456,
        "rtt_avg_ms": 0.845,
        "rtt_list_ms": [0.234, 0.845, 1.456]
    }
}
```

Failed ping result:
```python
{
    "status": "error",
    "data": {
        "target": "192.168.1.99",
        "packets_sent": 3,
        "packets_received": 0,
        "packet_loss_percent": 100.0,
        "error": "Host unreachable"
    }
}
```

Permission error result:
```python
{
    "status": "error",
    "data": {
        "error": "Insufficient permissions for ICMP sockets. "
                 "Run with CAP_NET_RAW capability or as root."
    }
}
```

### 3.3 Implementation Approach

**Option A: Use `icmplib` library (RECOMMENDED)**
- Pure Python ICMP implementation
- Cross-platform support
- Requires privileges but handles gracefully
- Active maintenance and good documentation

**Option B: Use system `ping` command**
- Shell out to system ping binary
- No privilege issues (ping binary has setuid)
- Platform-specific command-line arguments
- Harder to parse output reliably

**Option C: Raw sockets with `asyncio`**
- Maximum control and efficiency
- More complex implementation
- Privilege handling complexity
- More testing burden

**Recommendation:** Use `icmplib` for initial implementation. It provides a good balance between functionality, reliability, and ease of implementation.

### 3.4 Module Structure

Create new file: `src/nyxmon/adapters/runner/executors/ping_executor.py`

```python
"""ICMP ping check executor."""

from icmplib import async_ping
from ....domain import Check, Result, ResultStatus

class PingCheckExecutor:
    """Executor for ICMP ping checks."""

    async def execute(self, check: Check) -> Result:
        """Execute a ping check."""
        # Implementation details

    async def aclose(self) -> None:
        """No resources to clean up."""
        pass
```

### 3.5 Registration

Update `src/nyxmon/adapters/runner/async_runner.py`:

```python
def _preregister_executors(self) -> None:
    # ... existing HTTP and DNS executors ...

    # Register ping executor
    from .executors.ping_executor import PingCheckExecutor
    ping_executor = PingCheckExecutor()
    self.executor_registry.register(CheckType.PING, ping_executor)
```

## 4. Implementation Plan

### Phase 1: Basic Implementation
1. Add `icmplib` dependency to `pyproject.toml`
2. Create `ping_executor.py` with basic ping functionality
3. Register executor in `async_runner.py`
4. Add unit tests for basic ping success/failure

### Phase 2: Error Handling
1. Implement timeout handling
2. Implement DNS resolution
3. Handle permission errors gracefully
4. Add tests for error cases

### Phase 3: Result Data
1. Collect RTT statistics (min/max/avg)
2. Track packet loss
3. Format result data according to spec
4. Add tests for result data structure

### Phase 4: Documentation
1. Update `docs/configuration.md` with ping check examples
2. Add ping check to `docs/usage.md`
3. Update API documentation if needed
4. Add example checks to `create_devdata` command (optional)

## 5. Testing Strategy

### 5.1 Unit Tests

Create `tests/unit/test_ping_executor.py`:

- Test successful ping to localhost/127.0.0.1
- Test timeout handling (unreachable host)
- Test DNS resolution
- Test result data structure
- Test error handling (permission errors)
- Mock `icmplib` for tests that can't run without privileges

### 5.2 Integration Tests

Add to `tests/e2e/test_ping_checks.py`:

- Test executor registration
- Test ping check execution through message bus
- Test result persistence
- Test check scheduling

### 5.3 Manual Testing

- Test on Linux (with and without CAP_NET_RAW)
- Test on macOS
- Test on Windows (if supported)
- Test with various network conditions (reachable/unreachable hosts)

## 6. Dependencies

### 6.1 New Dependencies

Add to `pyproject.toml`:

```toml
dependencies = [
    # ... existing dependencies ...
    "icmplib>=3.0.0",
]
```

### 6.2 Privilege Requirements

Document in deployment guides:

**Linux:**
- Option 1: Run agent with CAP_NET_RAW capability
  ```bash
  sudo setcap cap_net_raw+ep /path/to/python
  ```
- Option 2: Run agent as root (not recommended for production)

**macOS:**
- Ping checks require root or admin privileges
- Consider using launchd with appropriate permissions

**Windows:**
- Ping checks require administrator privileges
- Run agent as administrator or use Windows service with appropriate privileges

## 7. Open Questions

1. **Q:** Should we support IPv6 ping checks?
   **A:** Yes, `icmplib` supports IPv6. Should work automatically if hostname resolves to IPv6.

2. **Q:** Should we implement fallback to system `ping` command if ICMP sockets fail?
   **A:** No, keep it simple initially. Users can configure their environment for proper privileges.

3. **Q:** Should ping checks be enabled by default in `create_devdata`?
   **A:** No, because they require privileges. Document separately.

4. **Q:** What should be the default timeout and count values?
   **A:** timeout=5s, count=3, interval=1s (standard ping defaults)

## 8. Security Considerations

1. **ICMP Flood Prevention:** Rate-limit ping checks at the application level if needed
2. **Hostname Injection:** Validate hostname format before DNS resolution
3. **Privilege Escalation:** Document privilege requirements clearly; never run entire agent as root if avoidable
4. **Network Scanning:** Consider rate limiting or restrictions to prevent abuse as a network scanning tool

## 9. Future Enhancements

- Support for custom ICMP packet sizes
- Support for TTL (time-to-live) configuration
- Traceroute-style path analysis
- MTU path discovery
- IPv6 explicit support and configuration

## 10. References

- [icmplib documentation](https://github.com/ValentinBELYN/icmplib)
- [RFC 792 - Internet Control Message Protocol](https://www.rfc-editor.org/rfc/rfc792)
- Python asyncio documentation
- NyxMon architecture documentation: `docs/architecture.md`
- Async runner documentation: `docs/async-check-runner.md`
