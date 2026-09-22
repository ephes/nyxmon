# DNS Monitoring Design Document

## Executive Summary

This document outlines the requirements and conceptual design for adding DNS monitoring capabilities to nyxmon. The feature will enable monitoring of DNS infrastructure by validating that DNS queries return expected IP addresses from specific network interfaces, supporting split-horizon DNS configurations. This design builds upon and refines the concepts outlined in `docs/dns-checks.md`.

## 1. Requirements

### 1.1 Functional Requirements

#### Core DNS Check Capabilities
1. **DNS Resolution Checks**: Ability to query a DNS server and verify the response
2. **Expected IP Validation**: Validate that the resolved IP address matches an expected value
3. **Network Interface Selection**: Query DNS from specific network interfaces (LAN, Tailscale, etc.)
4. **Timeout Handling**: Configurable timeout for DNS queries

#### Specific Check Requirements
1. **LAN Check**: Resolving `home.xn--wersdrfer-47a.de` from the local LAN should return `192.168.178.94`
2. **Tailscale Check**: Resolving `home.xn--wersdrfer-47a.de` from the Tailscale interface should return `100.119.21.93`
3. **Public DNS Check**: (Optional) Resolving `home.xn--wersdrfer-47a.de` from public DNS should return the current dialup IP

### 1.2 Non-Functional Requirements

1. **Architecture Consistency**: Follow the existing DDD architecture with message bus, commands, and events
2. **Async Execution**: DNS checks should be executed asynchronously like existing HTTP checks
3. **Error Handling**: Comprehensive error handling for DNS failures, timeouts, and network errors
4. **Performance**: DNS checks should complete within reasonable timeframes (default 5 seconds)
5. **Extensibility**: Design should allow for future DNS check types (MX, TXT, AAAA records)

## 2. Conceptual Design

### 2.1 Architecture Overview

The DNS monitoring feature will follow nyxmon's existing domain-driven design pattern:

```
Domain Layer (Models & Events)
    ↓
Service Layer (Command Handlers)
    ↓
Adapter Layer (DNS Check Executor)
    ↓
Infrastructure (DNS Resolution Libraries)
```

### 2.2 Domain Model Extensions

#### Check Type Extension
The existing `CheckType` enum already includes `DNS = "dns"`, which we'll utilize.

#### DNS Check Data Structure
DNS checks will store configuration in the `Check.data` field:
```python
{
    "expected_ips": ["192.168.178.94"],  # Required: List of expected IP addresses
    "dns_server": "192.168.178.94",      # Optional: DNS server to query (uses system default if not specified)
    "source_ip": "192.168.178.50",       # Optional: Source IP to bind for the query (not interface name)
    "query_type": "A",                    # Optional: Record type (default: "A")
    "timeout": 5.0                        # Optional: Query timeout in seconds (default: 5.0)
}
```

### 2.3 Component Design

#### 2.3.1 DNS Check Executor

Create a new DNS check executor parallel to the existing HTTP check logic in the `AsyncCheckRunner`:

**Location**: `src/nyxmon/adapters/runner/dns_executor.py`

**Responsibilities**:
- Parse DNS check configuration from `Check.data`
- Execute DNS queries using appropriate Python DNS library (dnspython recommended)
- Bind to specific source IP address if specified (not interface name)
- Compare resolved IPs with expected values
- Return appropriate `Result` with status and metadata

#### 2.3.2 Modified AsyncCheckRunner

The `AsyncCheckRunner` needs to be extended to support multiple check types:

**Current State**: Only executes HTTP checks using `httpx`

**Proposed State**:
- Route checks to appropriate executors based on `check.check_type`
- Support both HTTP and DNS checks (and future types)
- Maintain existing async/await pattern

### 2.4 Implementation Strategy

#### Phase 1: Core DNS Check Implementation
1. Create DNS executor module with basic A record resolution
2. Modify AsyncCheckRunner to support check type routing
3. Implement source IP binding capability (using IP addresses, not interface names)

#### Phase 2: Validation and Error Handling
1. Add expected IP validation
2. Implement comprehensive error handling
3. Add timeout configuration

#### Phase 3: Frontend Integration
1. Add DNS check configuration UI
2. Display DNS check results appropriately
3. Add DNS-specific error messages

### 2.5 Technical Considerations

#### DNS Library Selection

**Recommendation**: `dnspython` library
- **Pros**:
  - Mature, well-maintained library
  - Supports async operations (dns.asyncquery)
  - Supports source IP binding (via source parameter)
  - Full DNS protocol support
- **Cons**:
  - Additional dependency
  - Only supports source IP binding, not interface name binding

#### Source IP Binding

For split-horizon DNS testing, we need to query from specific source IP addresses:

1. **LAN Source IP**: Use an IP from the LAN range (e.g., 192.168.178.50)
2. **Tailscale Source IP**: Use an IP from the Tailscale range (e.g., 100.119.21.50)
3. **Important Limitation**: dnspython only supports source IP binding, not interface name binding
4. **Configuration Requirement**: Users must provide actual IP addresses, not interface names
5. **Future Enhancement**: Could add a helper to discover available source IPs from interfaces

#### Async Implementation Pattern

```python
async def execute_dns_check(check: Check) -> Result:
    """Execute a single DNS check."""
    config = check.data

    # Parse configuration
    expected_ips = config.get("expected_ips")  # Required
    dns_server = config.get("dns_server")      # Optional
    query_type = config.get("query_type", "A")
    source_ip = config.get("source_ip")        # Optional source IP address
    timeout = config.get("timeout", 5.0)

    # Execute DNS query with optional source IP binding
    # Compare results with expected IPs
    # Return Result object
```

### 2.6 Result Data Structure

DNS check results will include detailed information:

```python
# Success case
{
    "resolved_ips": ["192.168.178.94"],
    "query_time_ms": 23,
    "dns_server_used": "192.168.178.94",
    "record_type": "A"
}

# Failure case
{
    "error_type": "resolution_mismatch",
    "expected": ["192.168.178.94"],
    "actual": ["192.168.178.95"],
    "query_time_ms": 45,
    "dns_server_used": "192.168.178.94"
}
```

### 2.7 Integration Points

1. **Check Creation**: Extend existing check creation to support DNS check configuration
2. **Check Execution**: Modify `AsyncCheckRunner._run_one()` to route DNS checks
3. **Result Processing**: Existing result handling infrastructure can be reused
4. **Notification**: DNS failures will trigger notifications like HTTP failures

## 3. Benefits of This Design

1. **Minimal Disruption**: Extends existing architecture rather than replacing it
2. **Consistency**: Follows established patterns in the codebase
3. **Flexibility**: Supports various DNS check scenarios
4. **Future-Proof**: Easy to add more DNS record types or check variations
5. **Testability**: Can mock DNS responses for unit testing

## 4. Risks and Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| Network interface permissions | DNS queries might fail on certain interfaces | Implement fallback to default interface |
| DNS library compatibility | Library might not work on all platforms | Abstract DNS operations behind interface |
| Performance impact | DNS checks might slow down check execution | Use async operations and reasonable timeouts |
| Complex configuration | Users might find DNS checks hard to configure | Provide sensible defaults and clear documentation |

## 5. Success Criteria

1. Successfully query DNS and validate responses
2. Support split-horizon DNS scenarios (different IPs from different interfaces)
3. Integrate seamlessly with existing monitoring infrastructure
4. Maintain system performance and reliability
5. Provide clear error messages for troubleshooting

## 6. Next Steps

After review and approval of this concept:

1. Create detailed implementation plan with specific tasks
2. Set up development environment with test DNS servers
3. Implement core DNS executor
4. Extend AsyncCheckRunner for multi-type support
5. Add unit and integration tests
6. Update frontend for DNS check configuration
7. Document DNS check configuration and usage

## Appendix A: Example DNS Check Configurations

### LAN DNS Check
```json
{
    "name": "DNS - home.wersdoerfer.de from LAN",
    "check_type": "dns",
    "url": "home.xn--wersdrfer-47a.de",
    "data": {
        "expected_ips": ["192.168.178.94"],
        "dns_server": "192.168.178.94",
        "source_ip": "192.168.178.50",  // Must be actual LAN IP of monitoring host
        "query_type": "A"
    }
}
```

### Tailscale DNS Check
```json
{
    "name": "DNS - home.wersdoerfer.de from Tailscale",
    "check_type": "dns",
    "url": "home.xn--wersdrfer-47a.de",
    "data": {
        "expected_ips": ["100.119.21.93"],
        "dns_server": "100.119.21.93",
        "source_ip": "100.119.21.50",  // Must be actual Tailscale IP of monitoring host
        "query_type": "A"
    }
}
```

Note: The source_ip must be an actual IP address assigned to the monitoring host, not an interface name.