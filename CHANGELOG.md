# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
