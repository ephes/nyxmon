# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Persistent warning/error streaks now send reminder notifications after a
  configurable number of additional failures (`NYXMON_NOTIFY_REPEAT_FAILURES`,
  default `12`).
- Immediate processing-lease alerts now have a per-check cooldown
  (`NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`, default `3600`).
- The collector now reclaims checks abandoned in `processing` after a
  configurable lease (`NYXMON_PROCESSING_LEASE_SECONDS`, default `900`) and
  records an immediate `stale_processing_lease` alert.
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

### Fixed
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
- Abandoned-batch reminders now use an independent unit of work, remain visible
  when affected checks are disabled, bypass ordinary cooldown and maintenance
  suppression, and retry failed attempts with a bounded backoff.
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
