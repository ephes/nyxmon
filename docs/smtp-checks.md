# SMTP Check Executor

NyxMon's SMTP executor sends a real message through an authenticated SMTP relay and marks the check as successful once the server accepts the message after `DATA`.

## Configuration

Store the executor settings in `check.data`:

```python
{
    "host": "smtp.home.wersdoerfer.de",
    "port": 587,
    "tls": "starttls",                  # "none" | "starttls" | "implicit"
    "username": "monitor@xn--wersdrfer-47a.de",
    "password_secret": "nyxmon_local_monitor_password",  # or use "password"
    "from_addr": "monitor@xn--wersdrfer-47a.de",
    "to_addr": "wersdoerfer.mailmon@gmail.com",
    "subject_prefix": "[nyxmon-outbound]",
    "timeout": 30,
    "retries": 2,
    "retry_delay": 5
}
```

Validation rules:

- `host`, `from_addr`, `to_addr`, and `subject_prefix` are required.
- `tls` must be one of `none`, `starttls`, or `implicit`.
- If `username` is set, either `password` or `password_secret` must be provided.
- `retries` and `retry_delay` must be zero or positive.

## Behaviour

- Performs STARTTLS or implicit TLS when requested.
- Authenticates before sending when credentials are provided.
- Builds subjects as `<prefix> <UTC timestamp> <6-char token>` and returns the token for IMAP correlation.
- Retries on 4xx responses (greylisting/temporary failures) with configurable backoff.
- Fails fast on 5xx errors, auth failures, connection errors, and timeouts.
- Drives the IMAP inbound check: keep `subject_prefix` in sync with the IMAP `search_subject`, and send at least once per IMAP interval so the IMAP check can find a fresh message.

## Integration Test Plan (Mail Monitoring PRD)

1. **Prerequisites**
   - Postfix running on macmini with submission (`587`) and STARTTLS enabled.
   - Monitor mailbox credentials (local `monitor@` + Gmail app password) available in secrets.
   - Postgrey enabled on the edge to exercise greylisting.
2. **Happy-path send**
   - Create a check with the configuration above and set `retries=2`, `retry_delay=5`.
   - Execute once via a quick harness:
     ```bash
     PYTHONPATH=src uv run python - <<'PY'
     import anyio
     from nyxmon.domain import Check, CheckType
     from nyxmon.adapters.runner.executors.smtp_executor import SmtpCheckExecutor

     check = Check(
         check_id=1,
         service_id=1,
         check_type=CheckType.SMTP,
         url="smtp.home.wersdoerfer.de",
         data={
             "host": "smtp.home.wersdoerfer.de",
             "port": 587,
             "tls": "starttls",
             "username": "monitor@xn--wersdrfer-47a.de",
             "password": "<app-password>",
             "from_addr": "monitor@xn--wersdrfer-47a.de",
             "to_addr": "wersdoerfer.mailmon@gmail.com",
             "subject_prefix": "[nyxmon-outbound]",
         },
     )

     async def main():
         executor = SmtpCheckExecutor()
         result = await executor.execute(check)
         print(result.status, result.data)
         await executor.aclose()

     anyio.run(main)
     PY
     ```
   - Verify `result.status == "ok"` and Postfix logs show acceptance on `DATA`.
3. **Greylisting path**
   - Enable Postgrey, clear its cache, and repeat. Expect one retry with `error_type="temporary_failure"` on the first attempt and success on the second/third.
   - Confirm the final result reports `attempts` > 1 and includes the correlation token for the IMAP check.
4. **Failure modes**
   - Provide an invalid password to confirm `error_type="auth_error"` and no retries.
   - Stop Postfix to confirm a single `connection_error`.

Record findings in `bd` comments when running these steps against staging/live mail.
