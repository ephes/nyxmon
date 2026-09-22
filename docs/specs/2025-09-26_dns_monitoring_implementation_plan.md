# DNS Monitoring Implementation Plan

## Overview

This plan focuses on implementing the core business logic for DNS monitoring in nyxmon, following the DDD architecture and message bus pattern. UI representation will be addressed in a future phase.

## Key Learnings from docs/dns-checks.md

Based on the existing dns-checks.md document, we should incorporate:

1. **Pluggable Executor Registry**: Instead of modifying AsyncCheckRunner directly, create a registry pattern for check executors
2. **DnsCheckConfig Helper**: Create a typed helper class for DNS check configuration validation and serialization
3. **Structured DNS Observations**: Capture detailed DNS metadata (questions, RRset, response codes, latency)
4. **Source Binding**: Consider using aiodns or dnspython's async APIs with source address binding
5. **Multiple Resolver Contexts**: Model LAN and Tailscale as separate checks for the same service

## Implementation Tasks

### Phase 1: Core DNS Resolution Logic

#### Task 1.1: Add dnspython dependency
- Add `dnspython` to project dependencies via `uv pip install dnspython`
- Update pyproject.toml

#### Task 1.2: Create DnsCheckConfig helper class
**File**: `src/nyxmon/domain/dns_config.py`

**Implementation**:
```python
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class DnsCheckConfig:
    """Typed configuration for DNS checks.

    Schema:
    - expected_ips: Required. List of IP addresses we expect to receive
    - dns_server: Optional. DNS server to query (uses system default if not specified)
    - source_ip: Optional. Source IP address to bind for the query (for split-horizon testing)
    - query_type: Optional. DNS record type (default: "A")
    - timeout: Optional. Query timeout in seconds (default: 5.0)
    """
    expected_ips: List[str]
    dns_server: Optional[str] = None  # Uses system resolver if not specified
    source_ip: Optional[str] = None    # Source IP to bind to (not interface name)
    query_type: str = "A"
    timeout: float = 5.0

    @classmethod
    def from_dict(cls, data: dict) -> 'DnsCheckConfig':
        """Deserialize from check.data dictionary."""

    def to_dict(self) -> dict:
        """Serialize to check.data dictionary."""
```

#### Task 1.3: Create DNS executor module
**File**: `src/nyxmon/adapters/runner/executors/dns_executor.py`

**Implementation**:
```python
import dns.asyncresolver
from ....domain import Check, Result, ResultStatus
from ....domain.dns_config import DnsCheckConfig

class DnsCheckExecutor:
    async def execute(self, check: Check) -> Result:
        """Execute a DNS check and return a Result with structured observations."""
        # Extract configuration from check.data
        config = DnsCheckConfig.from_dict(check.data)
        # Use check.url as the domain to query
        domain = check.url
        # Perform DNS resolution...
```

**Key responsibilities**:
- Accept Check object following CheckExecutor protocol
- Extract DnsCheckConfig from check.data
- Use check.url as the domain to query
- Create resolver with specified DNS server
- Execute async DNS query with optional source IP binding
- Capture detailed DNS metadata (RRset, response codes, latency)
- Validate resolved IPs against expected values
- Return Result with structured observations

#### Task 1.4: Implement source IP binding
**Functionality**:
- Support binding DNS queries to specific source IP addresses
- Note: dnspython only supports source IP binding, not interface name binding
- Implement fallback to default source if binding fails

**Technical approach**:
- For LAN check: Configure source_ip as a LAN IP (e.g., "192.168.178.50")
- For Tailscale check: Configure source_ip as a Tailscale IP (e.g., "100.119.21.50")
- Use dnspython's source parameter in resolver configuration

**Important considerations**:
- Source IP must be a valid IP address assigned to the monitoring host
- Interface name to IP resolution is out of scope (must be configured explicitly)
- Document that users need to provide the actual source IP, not interface name
- Consider adding a helper utility in future to discover available source IPs

### Phase 2: Create Pluggable Executor Registry

#### Task 2.1: Create executor registry and interface
**File**: `src/nyxmon/adapters/runner/executors/__init__.py`

**Implementation**:
```python
from typing import Protocol, Dict, Type
from ....domain import Check, Result

class CheckExecutor(Protocol):
    async def execute(self, check: Check) -> Result:
        """Execute a check and return a Result."""

class ExecutorRegistry:
    def __init__(self):
        self._executors: Dict[str, CheckExecutor] = {}

    def register(self, check_type: str, executor: CheckExecutor):
        self._executors[check_type] = executor

    def get_executor(self, check_type: str) -> CheckExecutor:
        return self._executors.get(check_type)
```

#### Task 2.2: Create HTTP executor following the new pattern
**File**: `src/nyxmon/adapters/runner/executors/http_executor.py`

**Implementation**:
- Extract existing HTTP logic from AsyncCheckRunner
- Implement CheckExecutor protocol
- Maintain backward compatibility

#### Task 2.3: Refactor AsyncCheckRunner to use executor registry
**File**: `src/nyxmon/adapters/runner/async_runner.py`

**Changes**:
- Initialize executor registry in constructor
- Register HTTP and DNS executors
- Delegate to appropriate executor based on check_type

**Implementation pattern**:
```python
async def _run_one(self, client, check, send_channel):
    executor = self.executor_registry.get_executor(check.check_type)
    if executor:
        result = await executor.execute(check)
    else:
        # Fallback to HTTP for backward compatibility
        result = await self._run_http_check(client, check)

    await send_channel.send(result)
```

### Phase 3: Domain Model Integration

#### Task 3.1: Verify CheckType.DNS support
**File**: `src/nyxmon/domain/models.py`
- Confirm DNS check type is properly defined
- No changes needed as CheckType.DNS already exists

#### Task 3.2: Define DNS check data schema
**Documentation**: Define expected structure for check.data when check_type is "dns"
```python
{
    "expected_ips": List[str],   # Required: List of expected IP addresses
    "dns_server": str,           # Optional: IP address of DNS server
    "source_ip": str,            # Optional: Source IP address to bind to
    "query_type": str,           # Optional: DNS record type (default: "A")
    "timeout": float             # Optional: Query timeout in seconds (default: 5.0)
}
```

### Phase 4: Error Handling and Result Structure

#### Task 4.1: Implement comprehensive error handling
**Error types to handle**:
- DNS timeout
- NXDOMAIN (domain not found)
- Connection refused (DNS server unreachable)
- Resolution mismatch (unexpected IPs)
- Source IP binding failure

#### Task 4.2: Define DNS result data structure with structured observations
**Success result.data**:
```python
{
    "resolved_ips": ["192.168.178.94"],
    "query_time_ms": 23,
    "dns_server": "192.168.178.94",
    "source_address": "192.168.178.50",
    "rrset": ["192.168.178.94"],  # Full answer RRset
    "response_code": "NOERROR",
    "questions": ["home.xn--wersdrfer-47a.de. IN A"]
}
```

**Failure result.data**:
```python
{
    "error_type": "resolution_mismatch",
    "expected": ["192.168.178.94"],
    "actual": ["192.168.178.95"],
    "dns_server": "192.168.178.94",
    "response_code": "NOERROR",
    "rrset": ["192.168.178.95"],
    "questions": ["home.xn--wersdrfer-47a.de. IN A"]
}
```

### Phase 5: Testing

#### Task 5.1: Unit tests for DNS executor
**File**: `tests/unit/test_dns_executor.py`
- Test successful DNS resolution with mocked resolver
- Test resolution mismatch detection
- Test timeout handling
- Test invalid DNS server handling
- Test source IP binding validation

#### Task 5.2: End-to-end tests for DNS checks
**File**: `tests/e2e/test_dns_checks.py`
- Test complete flow from ExecuteChecks command
- Test DNS check through AsyncCheckRunner
- Test with mocked DNS responses for predictable testing
- Verify result storage and retrieval
- Test error cases and fallback behavior

### Phase 6: Configuration and Bootstrap

#### Task 6.1: Update bootstrap for DNS support
**File**: `src/nyxmon/bootstrap.py`
- Ensure DNS checks are properly initialized
- No major changes expected due to existing architecture

#### Task 6.2: Add DNS check examples
**Documentation**: Create example DNS check configurations
- LAN check configuration
- Tailscale check configuration
- Document required fields and optional parameters

## Implementation Order

1. **Add dnspython dependency** (Task 1.1) - Required for DNS executor
2. **Create DnsCheckConfig helper** (Task 1.2) - Type-safe configuration
3. **Create executor registry interface** (Task 2.1) - Pluggable architecture
4. **Extract HTTP executor** (Task 2.2) - Refactor existing code
5. **Create DNS executor** (Task 1.3) - Core DNS functionality
6. **Add unit tests for executors** (Task 5.1) - Verify core functionality
7. **Integrate executor registry into AsyncCheckRunner** (Task 2.3)
8. **Add source IP binding** (Task 1.4) - Advanced feature
9. **Complete error handling** (Task 4.1)
10. **Add e2e tests** (Task 5.2)
11. **Documentation and examples** (Task 6.2)

## Key Design Decisions

### 1. Pluggable Executor Registry
- Create extensible architecture for different check types
- Avoid modifying AsyncCheckRunner for each new check type
- Follow Open/Closed Principle

### 2. Typed Configuration
- Use DnsCheckConfig dataclass for type safety
- Validate configuration at domain layer
- Avoid raw dictionary access throughout codebase

### 3. Structured DNS Observations
- Capture full DNS metadata (RRset, response codes, questions)
- Enable detailed debugging and diagnostics
- Support future alerting on degraded responses

### 4. Separate Checks for Different Contexts
- Model LAN and Tailscale as separate checks
- Each check has its own configuration and expectations
- Clearer separation of concerns

### 5. Source Address Binding
- Use source IP binding for network interface selection
- More reliable than interface name binding
- Determine source IP based on interface's IP range

### 6. Result Compatibility
- DNS check results use same Result model as HTTP checks
- Status is either OK or ERROR
- Detailed DNS-specific information in result.data field

## Testing Strategy

### Unit Tests (`tests/unit/`)
- Mock DNS resolver responses using unittest.mock
- Test DnsCheckConfig validation and serialization
- Test executor error handling independently
- Verify correct Result generation for all cases

### End-to-End Tests (`tests/e2e/`)
- Test complete message bus flow with DNS checks
- Use mocked DNS responses for predictable testing
- Verify integration with repositories and unit of work
- Test executor registry and check routing

### Manual Testing
- Test against actual DNS infrastructure
- Verify LAN resolution returns 192.168.178.94 when using LAN source IP
- Verify Tailscale resolution returns 100.119.21.93 when using Tailscale source IP
- Document source IP configuration requirements

## Success Criteria

1. ✓ DNS checks execute asynchronously alongside HTTP checks
2. ✓ Correct IP validation for LAN queries
3. ✓ Correct IP validation for Tailscale queries
4. ✓ Proper error handling and reporting
5. ✓ No disruption to existing HTTP checks
6. ✓ Comprehensive test coverage

## Dependencies

- **dnspython**: DNS resolution library with async support
- **Python 3.13+**: Already required by project
- **Network access**: For actual DNS queries during testing

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Interface binding requires elevated permissions | Implement fallback to default interface |
| DNS library platform compatibility | Abstract DNS operations behind interface |
| Async DNS might not work on all platforms | Provide sync fallback option |

## Next Steps

1. Review and approve this implementation plan
2. Begin implementation with Task 1.1 and 1.2
3. Iteratively build and test each component
4. Document any deviations from plan as they occur