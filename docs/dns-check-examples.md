# DNS Check Configuration Examples

This document provides example configurations for DNS monitoring checks in nyxmon.

## Configuration Structure

DNS checks use the following structure in the `check.data` field:

```python
{
    "expected_ips": ["192.168.178.94"],  # Required: List of expected IP addresses
    "dns_server": "192.168.178.94",      # Optional: DNS server to query (uses system default if not specified)
    "source_ip": "192.168.178.50",       # Optional: Source IP to bind for the query (not interface name)
    "query_type": "A",                    # Optional: Record type (default: "A")
    "timeout": 5.0                        # Optional: Query timeout in seconds (default: 5.0)
}
```

## Example Configurations

### Basic DNS Check

Query the default system DNS resolver to check if a domain resolves to expected IP:

```python
from nyxmon.domain import Check, CheckType

check = Check(
    check_id=1,
    name="Basic DNS - example.com",
    check_type=CheckType.DNS,
    url="example.com",
    data={
        "expected_ips": ["93.184.216.34"]
    },
    interval=300
)
```

### LAN DNS Check (Split-Horizon DNS)

Check that a domain resolves to the internal LAN IP when queried from a LAN interface:

```python
check = Check(
    check_id=2,
    name="DNS - home.wersdoerfer.de from LAN",
    check_type=CheckType.DNS,
    url="home.xn--wersdrfer-47a.de",
    data={
        "expected_ips": ["192.168.178.94"],
        "dns_server": "192.168.178.94",
        "source_ip": "192.168.178.50",  # Must be actual LAN IP of monitoring host
        "query_type": "A"
    },
    interval=60
)
```

**Important**: `source_ip` must be an actual IP address assigned to the monitoring host's LAN interface, not the interface name (e.g., not "eth0" or "en0").

### Tailscale DNS Check (Split-Horizon DNS)

Check that a domain resolves to the Tailscale IP when queried from a Tailscale interface:

```python
check = Check(
    check_id=3,
    name="DNS - home.wersdoerfer.de from Tailscale",
    check_type=CheckType.DNS,
    url="home.xn--wersdrfer-47a.de",
    data={
        "expected_ips": ["100.119.21.93"],
        "dns_server": "100.119.21.93",
        "source_ip": "100.119.21.50",  # Must be actual Tailscale IP of monitoring host
        "query_type": "A"
    },
    interval=60
)
```

### Custom DNS Server

Query a specific DNS server (e.g., Google DNS, Cloudflare DNS):

```python
# Google DNS
check = Check(
    check_id=4,
    name="DNS via Google DNS - example.com",
    check_type=CheckType.DNS,
    url="example.com",
    data={
        "expected_ips": ["93.184.216.34"],
        "dns_server": "8.8.8.8"
    },
    interval=300
)

# Cloudflare DNS
check = Check(
    check_id=5,
    name="DNS via Cloudflare - example.com",
    check_type=CheckType.DNS,
    url="example.com",
    data={
        "expected_ips": ["93.184.216.34"],
        "dns_server": "1.1.1.1"
    },
    interval=300
)
```

### Multiple Expected IPs

Check that DNS returns one of several acceptable IP addresses:

```python
check = Check(
    check_id=6,
    name="DNS - CDN with multiple IPs",
    check_type=CheckType.DNS,
    url="cdn.example.com",
    data={
        "expected_ips": [
            "192.0.2.1",
            "192.0.2.2",
            "192.0.2.3"
        ]
    },
    interval=300
)
```

The check will succeed if the DNS response contains **any** of the expected IPs.

### Custom Timeout

Set a custom timeout for DNS queries (useful for slow DNS servers):

```python
check = Check(
    check_id=7,
    name="DNS with custom timeout",
    check_type=CheckType.DNS,
    url="slow-dns.example.com",
    data={
        "expected_ips": ["192.0.2.1"],
        "timeout": 10.0  # 10 second timeout
    },
    interval=300
)
```

### AAAA (IPv6) Record Check

Check IPv6 addresses:

```python
check = Check(
    check_id=8,
    name="DNS - IPv6 check",
    check_type=CheckType.DNS,
    url="example.com",
    data={
        "expected_ips": ["2606:2800:220:1:248:1893:25c8:1946"],
        "query_type": "AAAA"
    },
    interval=300
)
```

## Using the CLI

You can add DNS checks using the `add-check` CLI tool:

```bash
# Add a basic DNS check
uv run add-check \
    --name "DNS - example.com" \
    --check-type dns \
    --url "example.com" \
    --interval 300 \
    --data '{"expected_ips": ["93.184.216.34"]}'

# Add a split-horizon DNS check for LAN
uv run add-check \
    --name "DNS - home from LAN" \
    --check-type dns \
    --url "home.xn--wersdrfer-47a.de" \
    --interval 60 \
    --data '{
        "expected_ips": ["192.168.178.94"],
        "dns_server": "192.168.178.94",
        "source_ip": "192.168.178.50"
    }'
```

## Result Data Structure

### Success Result

When a DNS check succeeds, the result includes:

```python
{
    "resolved_ips": ["192.168.178.94"],
    "query_time_ms": 23,
    "response_code": "NOERROR",
    "questions": ["home.xn--wersdrfer-47a.de. IN A"],
    "rrset": ["192.168.178.94"],
    "dns_server": "192.168.178.94",
    "source_address": "192.168.178.50"  # Only if source_ip was specified
}
```

### Failure Result (Resolution Mismatch)

When resolved IPs don't match expected:

```python
{
    "error_type": "resolution_mismatch",
    "expected": ["192.168.178.94"],
    "actual": ["192.168.178.95"],
    "query_time_ms": 45,
    "response_code": "NOERROR",
    "questions": ["home.xn--wersdrfer-47a.de. IN A"],
    "rrset": ["192.168.178.95"],
    "dns_server": "192.168.178.94"
}
```

### Failure Result (DNS Error)

When DNS query fails:

```python
{
    "error_type": "nxdomain",  # or "timeout", "no_answer", etc.
    "error_msg": "Domain example.invalid does not exist"
}
```

## Finding Your Source IP

To find the appropriate `source_ip` for your checks:

```bash
# List all network interfaces and their IPs
ip addr show        # Linux
ifconfig            # macOS/BSD

# Find your LAN IP (typically 192.168.x.x or 10.x.x.x)
ip addr show eth0   # Linux - replace eth0 with your interface

# Find your Tailscale IP (typically 100.x.x.x)
tailscale ip
```

Use the IP address shown, not the interface name (e.g., "192.168.178.50", not "eth0").

## Supported Query Types

The following DNS record types are supported via the `query_type` field:

- **A**: IPv4 address (default)
- **AAAA**: IPv6 address
- **MX**: Mail exchange records
- **TXT**: Text records
- **CNAME**: Canonical name
- **NS**: Name server
- **SOA**: Start of authority
- **PTR**: Pointer (reverse DNS)

## Best Practices

1. **Split-Horizon DNS**: Create separate checks for each network context (LAN, VPN, public)
2. **Intervals**: Use shorter intervals (60s) for critical internal services, longer (300s) for external
3. **Multiple IPs**: List all acceptable IPs when using load balancers or CDNs
4. **Timeouts**: Keep timeouts reasonable (5-10s) to avoid blocking other checks
5. **Documentation**: Document why specific source IPs are used in check names

## Troubleshooting

### "Source IP binding failed"

- Ensure the source IP is actually assigned to the monitoring host
- Check that you're using an IP address, not an interface name
- Verify network permissions allow binding to that source

### "DNS timeout"

- Check if the DNS server is reachable
- Increase the timeout value
- Verify firewall rules allow DNS (UDP/TCP port 53)

### "Resolution mismatch"

- Verify the expected IPs are correct
- Check if DNS records have changed
- For split-horizon DNS, ensure you're querying the correct DNS server with the correct source IP