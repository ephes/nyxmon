# Custom SSH-JSON Checks

Use the `custom` check type with `mode: ssh-json` to run a command on a remote host via SSH and evaluate threshold rules against the JSON output.

This is useful for executing custom scripts or commands that produce JSON metrics (e.g., storage health exporters) on hosts that are not directly accessible over HTTP.

## Configuration Schema

```python
{
    "type": "custom",
    "url": "user@host",  # fallback target if not specified in data
    "data": {
        "mode": "ssh-json",
        "target": "user@host",              # optional: SSH destination (defaults to check.url)
        "command": "/path/to/script",       # required: command to execute (str or list[str])
        "checks": [                          # required: threshold rules to evaluate
            {"path": "$.key", "op": "==", "value": "expected", "severity": "critical"}
        ],
        "timeout": 15.0,                    # optional: command timeout in seconds (default: 15)
        "retries": 0,                       # optional: retry count on SSH/timeout failures (default: 0)
        "retry_delay": 2.0,                 # optional: delay between retries in seconds (default: 2)
        "ssh_args": ["-o", "BatchMode=yes"] # optional: custom SSH arguments (default: ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"])
    }
}
```

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `mode` | `str` | Must be `"ssh-json"` |
| `command` | `str` or `list[str]` | Command to execute on the remote host. Can be a string (single command) or list of strings (command with arguments). |
| `checks` | `list[dict]` | Non-empty list of threshold rules to evaluate against the JSON output. |

## Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `target` | `str` | `check.url` | SSH destination in `user@host` format. Falls back to `check.url` if not specified. |
| `timeout` | `float` | `15.0` | Maximum time in seconds to wait for the SSH command to complete. |
| `retries` | `int` | `0` | Number of times to retry on SSH connection or timeout failures. |
| `retry_delay` | `float` | `2.0` | Seconds to wait between retry attempts. |
| `ssh_args` | `list[str]` | `["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]` | SSH command-line arguments. Use an explicit empty list `[]` to disable defaults. |

## Threshold Check Format

Each entry in `checks` must include:

| Key | Type | Description |
|-----|------|-------------|
| `path` | `str` | JSONPath to the value (e.g., `$.pools.tank.health` or `$.disks[0].ok`) |
| `op` | `str` | Comparison operator: `<`, `<=`, `>`, `>=`, `==`, `!=` |
| `value` | `any` | Expected value to compare against |
| `severity` | `str` | Either `"warning"` or `"critical"` |

## Example: Storage Health Check

```python
from nyxmon.domain import Check, CheckType

check = Check(
    check_id=13,
    service_id=6,
    name="Fractal Storage Health",
    check_type=CheckType.CUSTOM,
    url="root@fractal.fritz.box",
    check_interval=300,
    data={
        "mode": "ssh-json",
        "target": "root@fractal.fritz.box",
        "command": "/usr/local/bin/nyxmon-storage-metrics",
        "timeout": 15,
        "retries": 0,
        "checks": [
            {"path": "$.pools.fast.health", "op": "==", "value": "ONLINE", "severity": "critical"},
            {"path": "$.pools.tank.health", "op": "==", "value": "ONLINE", "severity": "critical"},
            {"path": "$.pools.fast.last_scrub_age_days", "op": "<", "value": 14, "severity": "warning"},
            {"path": "$.pools.tank.last_scrub_age_days", "op": "<", "value": 14, "severity": "warning"},
            {"path": "$.disks_by_name.boot-nvme.ok", "op": "==", "value": True, "severity": "critical"},
            {"path": "$.disks_by_name.fast-nvme.ok", "op": "==", "value": True, "severity": "critical"},
            {"path": "$.disks_by_name.tank-hdd-1.ok", "op": "==", "value": True, "severity": "critical"},
            {"path": "$.disks_by_name.tank-hdd-2.ok", "op": "==", "value": True, "severity": "critical"},
            {"path": "$.ecc.loaded", "op": "==", "value": True, "severity": "warning"},
        ],
    },
)
```

## Example: Command as List

When the remote command requires arguments with special characters, use a list:

```python
data={
    "mode": "ssh-json",
    "target": "user@host",
    "command": ["python3", "-c", "import json; print(json.dumps({'status': 'ok'}))"],
    "checks": [
        {"path": "$.status", "op": "==", "value": "ok", "severity": "critical"}
    ],
}
```

## JSONPath Syntax

The path resolver supports:
- Dot notation: `$.field.subfield`
- Bracket notation for arrays: `$.items[0].value` or `$.items.0.value`
- Root reference: `$` (returns entire payload)

Wildcards and escaped dots are not supported.

## Error Types

| `error_type` | Description |
|--------------|-------------|
| `configuration_error` | Invalid config (missing fields, wrong types, invalid operators) |
| `timeout` | SSH command exceeded timeout |
| `ssh_error` | SSH connection or command execution failed (non-zero exit) |
| `json_error` | Command output was not valid JSON |
| `threshold_failed` | One or more threshold checks failed |

## SSH Prerequisites

1. **SSH key authentication**: The Nyxmon host must have SSH key access to the target host (no password prompts).
2. **Known hosts**: The target host's key should be in `~/.ssh/known_hosts` or use `-o StrictHostKeyChecking=no`.
3. **BatchMode**: The default `ssh_args` include `-o BatchMode=yes` to prevent interactive prompts.

Example setup:
```bash
# On Nyxmon host (as the nyxmon user)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
ssh-copy-id root@target-host

# Test non-interactive access
ssh -o BatchMode=yes root@target-host -- /usr/local/bin/nyxmon-storage-metrics | jq .
```
