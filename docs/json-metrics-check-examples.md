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
