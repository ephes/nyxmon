# IMAP Check Examples

Use the `imap` check type to verify that expected messages arrive and can be cleaned up. Typical use: inbound flow for mail monitoring (`[nyxmon-outbound]` subjects forwarded to a local mailbox).

```python
from nyxmon.domain import Check, CheckType

check = Check(
    check_id=202,
    service_id=1,
    name="Inbound mail flow (local)",
    check_type=CheckType.IMAP,
    url="imap.home.wersdoerfer.de",  # host
    check_interval=900,
    data={
        "port": 993,
        "tls_mode": "implicit",  # "implicit", "starttls", "none"
        "verify": True,  # default; False skips certificate/hostname checks
        "username": "monitor@xn--wersdrfer-47a.de",
        "password_secret": "nyxmon_local_monitor_password",  # or password
        "folder": "INBOX",
        "search_subject": "[nyxmon-outbound]",
        "max_age_minutes": 120,
        "delete_after_check": False,
        "no_recent_message_severity": "warning",
        "timeout": 30,
        "retries": 2,
        "retry_delay": 10,
    },
)
```

Behavior:
- Connects with the chosen TLS mode (implicit/starttls/none), logs in, and selects `folder`.
- Implicit TLS and STARTTLS verify the server certificate chain and hostname against the system trust store before the password is sent. `verify` defaults to `true`; set it to `false` only for a server with a self-signed certificate. That disables both checks and logs a warning on every connection, and the login password is then exposed to a man-in-the-middle. A certificate failure is reported as `error_type="tls_error"` and is not retried.
- Searches undeleted messages matching `search_subject`, filters to those newer than `max_age_minutes`.
- Uses UID SEARCH/FETCH/STORE so another client expunging mail cannot shift the selected identity. A UID that disappears before FETCH is treated as missing.
- On success returns stable mailbox UIDs in `matched_uids` and `latest_internaldate`. `delete_after_check` defaults to true: matched messages are marked `\Deleted` and permanently removed only by UID EXPUNGE when the server advertises UIDPLUS. Without UIDPLUS they remain marked deleted (excluded from subsequent searches) until mailbox-side cleanup. Nyxmon never issues mailbox-wide EXPUNGE, preserving unrelated deleted mail. Set `delete_after_check` to false to retain matches without marking them.
- Failures surface as `error_type` values such as `no_recent_message`, `tls_error`, `timeout`, `request_error`, or `execution_error`; retries/backoff apply to transient failures and to empty recent-message searches before `no_recent_message` is returned.
- `no_recent_message_severity` defaults to `critical`; set it to `warning` for third-party forwarded loopbacks where missing fresh mail means the forwarding path is stale but local IMAP/auth/connectivity should not page.

Tips:
- Use app passwords or vault references via `password_secret`.
- Pair with the SMTP check that sends `[nyxmon-outbound]` messages; IMAP retries briefly for a just-sent loopback before returning `no_recent_message`.
- Keep `max_age_minutes` aligned with the SMTP check’s interval + expected delivery delay (consider greylisting on first contact).
- For forwarded loopback checks where delivery can jitter, keep `delete_after_check` disabled and use mailbox-side retention cleanup so one delayed cycle does not remove the evidence needed by the next poll.
- If subjects contain special characters, they are quoted in the IMAP search to avoid parsing issues.

Pairing guidance:
- Use the same `subject_prefix` in SMTP and `search_subject` in IMAP.
- If you forward via Gmail, deliveries may be delayed or skipped; retries and a freshness window well above the poll interval, such as 120 minutes for a 15-minute poll, cover multi-probe forwarding gaps.
- For Gmail-forwarded loopbacks, consider `no_recent_message_severity: "warning"` and keep a separate Gmail-independent inbound check critical.
- IMAP deletes matched messages when `delete_after_check` is true, so SMTP should continue sending at least once per interval to keep IMAP green.
