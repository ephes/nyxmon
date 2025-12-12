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
        "username": "monitor@xn--wersdrfer-47a.de",
        "password_secret": "nyxmon_local_monitor_password",  # or password
        "folder": "INBOX",
        "search_subject": "[nyxmon-outbound]",
        "max_age_minutes": 30,
        "delete_after_check": True,
        "timeout": 30,
        "retries": 2,
        "retry_delay": 10,
    },
)
```

Behavior:
- Connects with the chosen TLS mode (implicit/starttls/none), logs in, and selects `folder`.
- Searches undeleted messages matching `search_subject`, filters to those newer than `max_age_minutes`.
- On success returns `matched_uids` and `latest_internaldate`; when `delete_after_check` is true, messages are deleted/expunged.
- Failures surface as `error_type` values such as `no_recent_message`, `timeout`, `request_error`, or `execution_error`; retries/backoff apply to transient failures.

Tips:
- Use app passwords or vault references via `password_secret`.
- Pair with the SMTP check that sends `[nyxmon-outbound]` messages; IMAP will stay red with `no_recent_message` until a fresh outbound test mail is delivered.
- Keep `max_age_minutes` aligned with the SMTP check’s interval + expected delivery delay (consider greylisting on first contact).
- If subjects contain special characters, they are quoted in the IMAP search to avoid parsing issues.

Pairing guidance:
- Use the same `subject_prefix` in SMTP and `search_subject` in IMAP.
- If you forward via Gmail, initial deliveries may be delayed by greylisting; retries and a 30-minute window cover this.
- IMAP deletes matched messages when `delete_after_check` is true, so SMTP should continue sending at least once per interval to keep IMAP green.
