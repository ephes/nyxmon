# JSON Metrics Check Examples

Use the `json-metrics` check type to validate threshold rules against a JSON health endpoint (e.g. the mail metrics endpoint `/.well-known/health`).

```python
from nyxmon.domain import Check, CheckType

check = Check(
    check_id=101,
    service_id=1,
    name="Mail metrics (macmini)",
    check_type=CheckType.JSON_METRICS,
    url="http://macmini.tailde2ec.ts.net:9100/.well-known/health",
    check_interval=300,
    data={
        "url": "http://macmini.tailde2ec.ts.net:9100/.well-known/health",
        "auth": {"username": "nyxmon", "password": "secret"},
        "timeout": 10,
        "checks": [
            {"path": "$.mail.queue_total", "op": "<", "value": 100, "severity": "warning"},
            {"path": "$.mail.queue_total", "op": "<", "value": 500, "severity": "critical"},
            {"path": "$.services.postfix", "op": "==", "value": "active", "severity": "critical"},
        ],
    },
)
```

Key details:
- Check type: `json-metrics`
- Auth: optional HTTP basic auth (`auth: {username, password}`)
- Path resolver: simple `$.field.subfield` or list indices (`$.items.0.value`); no wildcards or escaped dots
- Retries: configurable `retries` + `retry_delay` for transient HTTP/timeout failures (defaults: 1 retry, 2s delay)
- Operators: `<`, `<=`, `>`, `>=`, `==`, `!=`
- Severities: `warning` or `critical`

Failures return `error_type=threshold_failed` with all failed rules in `failures`.

## Fractal Backup Endpoint Integration

For Fractal backup monitoring via the backup metrics endpoint (`/.well-known/backup`),
create a `json-metrics` check with these rules:

```python
check = Check(
    check_id=202,
    service_id=2,
    name="Fractal backup health",
    check_type=CheckType.JSON_METRICS,
    url="http://<FRACTAL_TAILSCALE_IP>:9103/.well-known/backup",
    check_interval=300,
    data={
        "url": "http://<FRACTAL_TAILSCALE_IP>:9103/.well-known/backup",
        "auth": {"username": "nyxmon", "password": "<backup_metrics_password>"},
        "timeout": 10,
        "retries": 1,
        "retry_delay": 2,
        "checks": [
            {"path": "$.summary.required_units_ok", "op": "==", "value": True, "severity": "critical"},
            {"path": "$.summary.local_snapshots_ok", "op": "==", "value": True, "severity": "critical"},
            {"path": "$.summary.overall_ok", "op": "==", "value": True, "severity": "critical"},
            {"path": "$.meta.age_seconds", "op": "<", "value": 600, "severity": "warning"},
        ],
    },
)
```

Optional USB policy signal:

- Add `{"path": "$.summary.usb_state", "op": "==", "value": "online", "severity": "warning"}`
  if you want a warning whenever USB is not currently imported/attached.

Important behavior:

- `$.summary.overall_ok` is the primary critical signal.
- The Fractal payload sets `usb_snapshots_ok` to `null` when USB is offline.
  Avoid checking `$.summary.usb_snapshots_ok == true` unless you intentionally want alerts while USB is offline.
