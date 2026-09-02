# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The monitoring CLI now keeps `httpx` and `httpcore` at `WARNING`, preventing
  their INFO request lines from writing Telegram bot tokens embedded in API
  URLs to journald. Legacy Linux deployment units read credentials from the
  mode-0600 environment file instead of embedding them in the unit and are
  explicitly enabled so they survive reboot.
- Legacy Linux deployment now starts Granian with the ordinary Django WSGI
  target instead of the version-specific `granian.utils.proxies` wrapper,
  limits stop waits to 30 seconds, and requires an HTTP 200 after restart.
  Ansible can no longer report a successful deploy while the web unit is dead.
- The `notification_suppression` freshness guard fails open on hostile numeric
  values instead of raising or suppressing. An arbitrarily large JSON integer
  made `float()` raise `OverflowError` and aborted result handling for the
  check, while `nan`, `-inf` and negative ages compared as "fresh" and
  permitted suppression from a value carrying no freshness information. An
  unusable `freshness_max_seconds` (overflowing, non-finite, boolean) fails
  open the same way.
- The in-memory store's compare-and-swap on notification state is now covered
  by regression tests for two forked units of work racing on the same
  snapshot, mirroring the SQLite atomic-rollback tests.
- `notification_suppression` now supports a `freshness_path` /
  `freshness_max_seconds` guard and fails open when the suppression source is
  stale. Where the suppression endpoint is the same one a check monitors, a
  frozen payload that captured a unit mid-run previously reported "still
  running" indefinitely and suppressed every later failure — including the
  staleness assertion meant to report the freeze, so the alert silenced exactly
  the condition it existed to detect. Configs without `freshness_path` are
  unchanged.

### Added
- Persistent warning/error incidents now send reminder notifications on
  elapsed wall-clock time rather than a sample count:
  `NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS` (default `21600`, six hours) for
  errors and `NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS` (default `86400`,
  24 hours) for warnings. A five-minute check and an hourly check now remind on
  the same schedule.
- Per-check alerting policy via `data.notification_policy`, supporting
  `consecutive_failures`, `reminder_seconds`, `warning_consecutive_failures`,
  and `warning_reminder_seconds`. Malformed or out-of-range values are warned
  about once per check and field and fall back to the global default, so a bad
  edit can neither raise a threshold silently nor disable alerting.
- Collector-level incidents are now persisted in a `collector_incident` table
  and deduplicated across collector iterations and process restarts. Reclaiming
  a batch of expired processing leases raises exactly one
  `collector:stale_processing_lease` alert with an hourly reminder, and a wedged
  executor raises exactly one `collector:execution_paused` alert, instead of one
  notification per affected check.
- Results that explicitly request an immediate alert through
  `data.notification_immediate` have a per-check cooldown
  (`NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`, default `3600`).
- The collector now reclaims checks abandoned in `processing` after a
  configurable lease (`NYXMON_PROCESSING_LEASE_SECONDS`, default `900`),
  records a `stale_processing_lease` result, and schedules the check again.
  Stale-lease reclaim is drained within a single collector iteration so a
  restart storm is recovered, and reported, in one go.
- Claim and stale-recovery batch size is configurable with
  `NYXMON_CHECK_BATCH_SIZE` (default `5`, range `1`–`100`).
  This replaces the previous hardcoded batch size of `100`; synchronized bursts
  can consequently incur more scheduling latency unless operators raise the
  setting for their workload.
- IMAP checks now support `no_recent_message_severity` so missing fresh messages
  can be warning-only for third-party forwarded loopback checks while other IMAP
  execution failures remain critical.
- Plain HTTP checks can now retry transient HTTP statuses (`502`, `503`, `504` by default), timeouts, connection errors, and request errors via `health_check.data`.
- Plain HTTP checks can disable redirect following and require an exact status
  and `Location` header, enabling continuous canonical-redirect contract checks.
- Notification dampening via `NYXMON_NOTIFY_CONSECUTIVE_FAILURES`, defaulting to 2 consecutive warning/error samples before Telegram or OpsGate side effects.
- Check-level `data.notification_suppression` can suppress Telegram and OpsGate
  side effects during active or recently finished maintenance windows while
  still storing the warning/error result.

### Changed
- Reclaimed processing leases no longer produce a per-check notification. The
  result is still stored so a check's history explains its gap, but it is marked
  collector-internal: it never alerts on its own, not even under a per-check
  `consecutive_failures: 1` policy, and it neither advances nor resets that
  check's own incident. The batch is reported once at collector level. This
  removes the failure mode where one wedged batch produced one Telegram message
  per reclaimed check.
- OpsGate tickets for collector incidents now use the task reference
  `nyxmon-collector-<incident_key>` instead of `nyxmon-check-0`, so a wedged
  executor and a stale-lease sweep no longer collide on one ticket while each
  still deduplicates across reminders and restarts.
- `NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS` now applies only to results that
  explicitly set `data.notification_immediate`. No production code path sets
  that flag any more.

### Deprecated
- `NYXMON_NOTIFY_REPEAT_FAILURES` is ignored. A sample count cannot be
  translated into a duration - twelve samples meant about one hour for a
  five-minute check and about twelve hours for an hourly one, which is the
  interval dependence the elapsed-time model removes - so no automatic
  conversion is attempted. If the variable is still set, the worker logs one
  warning per distinct value naming `NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS` and
  `NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS`, then continues with the
  defaults. Remove it from deployment configuration.

### Fixed
- Upgrade note: apply Django migration `0012_notification_reminder_timestamps`
  (normally run automatically by the deployment role) before starting the
  updated worker. It adds `last_notified_at` and `first_failure_at` to
  `check_notification_state` and creates the `collector_incident` table. The
  worker performs the same upgrade idempotently on start, so either order is
  safe. Existing failure streaks are adopted rather than re-paged: a check that
  was already failing and had already alerted gets `last_notified_at` stamped
  with the upgrade time, so its next reminder is one full window away, while a
  streak that had never reached the threshold keeps its normal first alert.
  Healthy checks are untouched, so the rollout produces no alert storm.
- Operators should either keep the SQLite database in a state directory of its
  own (for example `/var/lib/nyxmon/db.sqlite3`), outside any tree a deployment
  synchronises, or exclude the database *and every sidecar* from the sync while
  preventing it from rewriting the containing directory's ownership. A database inside the deployed source tree can lose a live
  `-journal`/`-wal`/`-shm` sidecar to a delete-enabled sync, and a sync that
  rewrites the containing directory's ownership makes SQLite fail with
  `attempt to write a readonly database` for the duration of the window even
  though the database file itself stays writable.
- Upgrade note: apply Django migration `0011_checknotificationstate` (normally
  run automatically by the deployment role) before starting the updated worker.
- Unexpected executor exceptions now become error results instead of leaving
  claimed checks permanently stuck in `processing`; detailed exception text is
  logged but no longer copied into stored results.
- Notification reminder state is now persisted in a dedicated internal table,
  preventing check edits, stale workers, or result-history cleanup from turning
  persistent failures into duplicate alerts.
- Untimestamped legacy processing claims now receive a full lease before
  recovery, and one failed stale-result write no longer blocks recovery of other
  checks in the same collector iteration.
- Abandoned-batch alerts no longer borrow an unrelated live check row. They are
  sent as a collector-scoped incident that remains visible when affected checks
  are disabled, is independent of per-check cooldown and maintenance
  suppression, and retries a failed send after one minute instead of waiting out
  the full reminder window.
- In-memory and SQLite result persistence now both refuse to resurrect checks
  deleted while an execution or lease recovery was in flight, preserve
  concurrent check edits, and suppress notifications for dropped results.
- Superseded late results remain in history but no longer change notification
  state or emit alerts, and bounded five-check batches keep worst-case serial
  notification time inside a result-handling allowance that scales with the
  configured batch size.
- Invalid reliability environment values now warn once per distinct value
  instead of flooding the worker log on every collector iteration or result.
- The effective processing lease now accounts for enabled checks' configured
  timeout/retry budgets, caps derived estimates at one hour, and prevents late
  results from overwriting a newer active claim.
- Runtime estimation rejects non-finite numeric values, and lease recovery uses
  repository views with independent event sets so a wedged batch cannot disable
  or cross-drain recovery work.
- Successful Telegram deliveries are now recorded in the worker log, making
  notification transport verification observable.
- Telegram HTTP failure logging now redacts the bot token while preserving the
  response status and bounded API error detail.
- IMAP checks now retry empty recent-message searches according to `retries` and `retry_delay` before returning `no_recent_message`.

## [0.1.7] - 2025-10-05

### Fixed
- **Package Build Issue (Again)**: The 0.1.6 wheel published to PyPI was built before the pyproject.toml fix was applied
  - Rebuilt wheel with correct configuration now properly includes all source code
  - Wheel size: 96KB (vs 3.8KB broken version)
  - Both `nyxmon` and `nyxboard` packages are now correctly included

### Changed
- **Build Process**: Must build wheel and sdist separately to avoid hatchling sdist-to-wheel build issues
  - Use `uv build --wheel && uv build --sdist` instead of just `uv build`
  - This ensures the wheel is built directly from source, not from the sdist

## [0.1.6] - 2025-10-05 [YANKED - Broken Build]

### Fixed
- **Critical Package Build Issue**: Fixed hatchling wheel build configuration to properly include both `nyxmon` and `nyxboard` packages
  - The 0.1.5 wheel was missing all Python source files, causing `ModuleNotFoundError: No module named 'nyxboard'`
  - Updated `pyproject.toml` to use `only-include` and `sources` directives for proper package discovery
  - Verified wheel contents include all necessary modules and templates

### Changed
- Build configuration: Added explicit `only-include` and `sources` settings in `[tool.hatch.build.targets.wheel]`

## [0.1.5] - 2025-10-05

### Added
- **DNS Health Check Support via Web Dashboard**
  - Type-specific forms for creating DNS, HTTP, and JSON-HTTP checks
  - Native HTML5 dropdown for check type selection (replaces manual URL editing)
  - DNS configuration fields: expected IPs, DNS server, source IP, query type, timeout
  - Form validation for DNS-specific fields with comprehensive error messages
  - Dedicated form templates with organized sections for better UX
  - Database schema support for check-type-specific configuration via JSONField

### Changed
- HealthCheck model: `url` field changed from URLField to CharField(512) to support bare domains
- HealthCheck model: Added `data` JSONField for storing type-specific configuration
- Repository implementations updated to handle DNS configuration serialization
- Form architecture: Introduced HttpHealthCheckForm, DnsHealthCheckForm, and GenericHealthCheckForm
- Template rendering: Only wrap HTTP URLs in anchor tags, render bare domains as plain text
- UI: Replaced single "Add Check" buttons with dropdown menus showing all check types

### Removed
- Bootstrap CSS/JS dependency (replaced with lightweight native CSS dropdowns)

### Fixed
- Confusing UX requiring manual URL editing to select check types
- Broken relative links for DNS checks with bare domain names
- Missing JSON-HTTP option in check type selectors

### Documentation
- Added "Creating Checks via the Dashboard" section to usage guide
- Added form-to-JSON mapping documentation for DNS checks
- Cross-referenced web UI workflow in dns-check-examples.md

### Testing
- Added 114 comprehensive form tests covering validation, tampering prevention, and data preservation
- Added repository tests for DNS configuration handling

## [0.1.4] - Previous release

_(Earlier versions not documented in changelog)_
