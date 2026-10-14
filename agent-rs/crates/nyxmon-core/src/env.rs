//! Environment variable readers (PLAN.md section 1.4).
//!
//! One function per variable (group), each a port of the Python reader named
//! in its doc comment. Values are `str.strip()`ped first, integers follow
//! Python `int()` and floats Python `float()` ([`crate::pyjson::py_int`],
//! [`crate::pyjson::py_float`]). Variables are read on every call, like
//! `os.environ.get` in Python.
//!
//! "Warn once" is per distinct `(variable, value)` for the lifetime of the
//! [`Env`], mirroring the `lru_cache`/`_warned_*` sets of the Python readers.
//! The OpsGate readers warn on every call, like Python (the notifier is built
//! once per process).
//!
//! Bounded readers return `i64` (their results always fit). The unbounded
//! integers (`NYXMON_PROCESSING_LEASE_SECONDS`,
//! `NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`,
//! `OPSGATE_TICKET_EXPIRES_SECONDS`) are returned exactly as [`BigInt`] like
//! Python's `int`, so callers can reproduce what Python does with values
//! beyond 64 bits (for example SQLite's `OverflowError` on binding them).
//!
//! A variable whose value is not valid UTF-8 is read lossily (U+FFFD). Python
//! would see surrogate escapes; both reject the value the same way and only
//! the `repr` in a site-connectivity warning differs.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::net::{Ipv4Addr, Ipv6Addr};
use std::sync::{Arc, Mutex};

use crate::log::{LogSink, TracingSink, loggers};
use crate::model::ResultStatus;
use num_bigint::BigInt;

use crate::pyjson::{
    PyNumError, bigint_to_i64_saturating, py_float, py_int, py_isspace, py_str_repr, py_strip,
};

/// Where variables are read from.
#[derive(Clone, Debug)]
pub enum EnvSource {
    /// The process environment.
    Process,
    /// A fixed map (tests).
    Map(HashMap<String, String>),
}

impl EnvSource {
    fn get(&self, name: &str) -> Option<String> {
        match self {
            EnvSource::Process => {
                std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
            }
            EnvSource::Map(map) => map.get(name).cloned(),
        }
    }
}

/// Variable source plus the process-wide warn-once state.
pub struct Env {
    source: EnvSource,
    warned: Mutex<HashSet<(String, String)>>,
    sink: Arc<dyn LogSink>,
}

impl fmt::Debug for Env {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Env").field("source", &self.source).finish()
    }
}

impl Env {
    pub fn new(source: EnvSource, sink: Arc<dyn LogSink>) -> Self {
        Env {
            source,
            warned: Mutex::new(HashSet::new()),
            sink,
        }
    }

    /// The process environment, warnings logged through `tracing`.
    pub fn process() -> Self {
        Env::new(EnvSource::Process, Arc::new(TracingSink))
    }

    /// A fixed map of variables (tests).
    pub fn from_map<K: Into<String>, V: Into<String>>(
        vars: impl IntoIterator<Item = (K, V)>,
        sink: Arc<dyn LogSink>,
    ) -> Self {
        let map = vars
            .into_iter()
            .map(|(k, v)| (k.into(), v.into()))
            .collect();
        Env::new(EnvSource::Map(map), sink)
    }

    /// Read variables from `source` from now on, keeping the warn-once
    /// state (tests that change the environment between calls).
    pub fn replace_source(&mut self, source: EnvSource) {
        self.source = source;
    }

    /// `os.environ.get(name)`.
    pub fn raw(&self, name: &str) -> Option<String> {
        self.source.get(name)
    }

    /// `os.environ.get(name, "").strip()`.
    fn stripped(&self, name: &str) -> String {
        self.raw(name)
            .map(|value| py_strip(&value).to_owned())
            .unwrap_or_default()
    }

    fn warn(&self, logger: &'static str, message: &str) {
        self.sink.warning(logger, message);
    }

    fn warn_once(
        &self,
        logger: &'static str,
        var: &str,
        value: &str,
        message: impl FnOnce() -> String,
    ) {
        let first = self
            .warned
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert((var.to_owned(), value.to_owned()));
        if first {
            self.warn(logger, &message());
        }
    }
}

/// `int(value)` clamped into `i64`; only for readers that compare the result
/// with small bounds, where clamping cannot change the outcome.
fn parse_int(value: &str) -> Option<i64> {
    py_int(value).ok().map(|n| bigint_to_i64_saturating(&n))
}

pub const DEFAULT_CHECK_BATCH_SIZE: i64 = 5;
pub const MAX_CHECK_BATCH_SIZE: i64 = 100;

/// `NYXMON_CHECK_BATCH_SIZE` (`repositories/interface.py:check_batch_size`).
pub fn check_batch_size(env: &Env) -> i64 {
    const VAR: &str = "NYXMON_CHECK_BATCH_SIZE";
    let value = env.stripped(VAR);
    if value.is_empty() {
        return DEFAULT_CHECK_BATCH_SIZE;
    }
    match parse_int(&value) {
        Some(parsed) => parsed.clamp(1, MAX_CHECK_BATCH_SIZE),
        None => {
            env.warn_once(loggers::REPOSITORY_INTERFACE, VAR, &value, || {
                format!("{VAR} is invalid; using default {DEFAULT_CHECK_BATCH_SIZE}")
            });
            DEFAULT_CHECK_BATCH_SIZE
        }
    }
}

pub const DEFAULT_PROCESSING_LEASE_SECONDS: i64 = 900;
pub const MIN_PROCESSING_LEASE_SECONDS: i64 = 30;

/// `NYXMON_PROCESSING_LEASE_SECONDS` (`collector.py:processing_lease_seconds`).
///
/// Unbounded above, so the exact Python integer is returned.
pub fn processing_lease_seconds(env: &Env) -> BigInt {
    const VAR: &str = "NYXMON_PROCESSING_LEASE_SECONDS";
    let value = env.stripped(VAR);
    if value.is_empty() {
        return BigInt::from(DEFAULT_PROCESSING_LEASE_SECONDS);
    }
    match py_int(&value) {
        Ok(parsed) if parsed < BigInt::from(MIN_PROCESSING_LEASE_SECONDS) => {
            env.warn_once(loggers::COLLECTOR, VAR, &value, || {
                format!("{VAR} is below 30; clamping to 30")
            });
            BigInt::from(MIN_PROCESSING_LEASE_SECONDS)
        }
        Ok(parsed) => parsed,
        Err(_) => {
            env.warn_once(loggers::COLLECTOR, VAR, &value, || {
                format!("{VAR} is invalid; using default {DEFAULT_PROCESSING_LEASE_SECONDS}")
            });
            BigInt::from(DEFAULT_PROCESSING_LEASE_SECONDS)
        }
    }
}

pub const DEFAULT_SHUTDOWN_GRACE_SECONDS: f64 = 5.0;

/// `NYXMON_SHUTDOWN_GRACE_SECONDS` (`collector.py:shutdown_grace_seconds`).
pub fn shutdown_grace_seconds(env: &Env) -> f64 {
    const VAR: &str = "NYXMON_SHUTDOWN_GRACE_SECONDS";
    let value = env.stripped(VAR);
    if value.is_empty() {
        return DEFAULT_SHUTDOWN_GRACE_SECONDS;
    }
    if let Ok(parsed) = py_float(&value)
        && parsed.is_finite()
        && (0.0..=30.0).contains(&parsed)
    {
        return parsed;
    }
    env.warn_once(loggers::COLLECTOR, VAR, &value, || {
        format!("{VAR} must be between 0 and 30; using 5")
    });
    DEFAULT_SHUTDOWN_GRACE_SECONDS
}

/// `notification_policy.py:_bounded_env_value`.
fn bounded_env_value(env: &Env, var: &str, default: i64, low: i64, high: i64) -> i64 {
    let value = env.stripped(var);
    if value.is_empty() {
        return default;
    }
    let logger = loggers::NOTIFICATION_POLICY;
    match parse_int(&value) {
        None => {
            env.warn_once(logger, var, &value, || {
                format!("{var} is invalid; using default {default}")
            });
            default
        }
        Some(parsed) if parsed < low || parsed > high => {
            env.warn_once(logger, var, &value, || {
                format!("{var} must be between {low} and {high}; using default {default}")
            });
            default
        }
        Some(parsed) => parsed,
    }
}

pub const DEFAULT_NOTIFY_CONSECUTIVE_FAILURES: i64 = 2;
pub const MAX_CONSECUTIVE_FAILURES: i64 = 100;
pub const DEFAULT_NOTIFY_REPEAT_INTERVAL_SECONDS: i64 = 6 * 3600;
pub const DEFAULT_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS: i64 = 24 * 3600;
pub const MIN_REMINDER_SECONDS: i64 = 60;
pub const MAX_REMINDER_SECONDS: i64 = 30 * 24 * 3600;
pub const ENV_CONSECUTIVE_FAILURES: &str = "NYXMON_NOTIFY_CONSECUTIVE_FAILURES";
pub const ENV_REPEAT_INTERVAL_SECONDS: &str = "NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS";
pub const ENV_WARNING_REPEAT_INTERVAL_SECONDS: &str =
    "NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS";
pub const ENV_DEPRECATED_REPEAT_FAILURES: &str = "NYXMON_NOTIFY_REPEAT_FAILURES";

/// `NYXMON_NOTIFY_CONSECUTIVE_FAILURES`
/// (`notification_policy.py:global_consecutive_failures`).
pub fn global_consecutive_failures(env: &Env) -> i64 {
    bounded_env_value(
        env,
        ENV_CONSECUTIVE_FAILURES,
        DEFAULT_NOTIFY_CONSECUTIVE_FAILURES,
        1,
        MAX_CONSECUTIVE_FAILURES,
    )
}

/// `notification_policy.py:_check_deprecated_env`: the deprecated variable is
/// ignored with one warning per distinct value.
fn check_deprecated_env(env: &Env) {
    let value = env.stripped(ENV_DEPRECATED_REPEAT_FAILURES);
    if value.is_empty() {
        return;
    }
    env.warn_once(
        loggers::NOTIFICATION_POLICY,
        ENV_DEPRECATED_REPEAT_FAILURES,
        &value,
        || {
            format!(
                "{ENV_DEPRECATED_REPEAT_FAILURES}={value} is deprecated and is IGNORED: reminder \
                 cadence is now elapsed-time based. A sample count cannot be translated to a \
                 duration because it means a different interval for every check. Set \
                 {ENV_REPEAT_INTERVAL_SECONDS} (and optionally \
                 {ENV_WARNING_REPEAT_INTERVAL_SECONDS}) instead."
            )
        },
    );
}

/// `NYXMON_NOTIFY_REPEAT_INTERVAL_SECONDS` /
/// `NYXMON_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS`
/// (`notification_policy.py:global_reminder_seconds`).
pub fn global_reminder_seconds(env: &Env, status: ResultStatus) -> i64 {
    check_deprecated_env(env);
    if status == ResultStatus::Warning {
        return bounded_env_value(
            env,
            ENV_WARNING_REPEAT_INTERVAL_SECONDS,
            DEFAULT_NOTIFY_WARNING_REPEAT_INTERVAL_SECONDS,
            MIN_REMINDER_SECONDS,
            MAX_REMINDER_SECONDS,
        );
    }
    bounded_env_value(
        env,
        ENV_REPEAT_INTERVAL_SECONDS,
        DEFAULT_NOTIFY_REPEAT_INTERVAL_SECONDS,
        MIN_REMINDER_SECONDS,
        MAX_REMINDER_SECONDS,
    )
}

pub const DEFAULT_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS: i64 = 3600;

/// `NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS`
/// (`handlers.py:_notify_immediate_cooldown_seconds`).
///
/// Unbounded above, so the exact Python integer is returned.
pub fn notify_immediate_cooldown_seconds(env: &Env) -> BigInt {
    const VAR: &str = "NYXMON_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS";
    let default = DEFAULT_NOTIFY_IMMEDIATE_COOLDOWN_SECONDS;
    let value = env.stripped(VAR);
    if value.is_empty() {
        return BigInt::from(default);
    }
    match py_int(&value) {
        Err(_) => {
            env.warn_once(loggers::HANDLERS, VAR, &value, || {
                format!("{VAR} is invalid; using default {default}")
            });
            BigInt::from(default)
        }
        Ok(parsed) if parsed <= BigInt::ZERO => {
            env.warn_once(loggers::HANDLERS, VAR, &value, || {
                format!("{VAR} must be positive; using default {default}")
            });
            BigInt::from(default)
        }
        Ok(parsed) => parsed,
    }
}

pub const DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS: i64 = 0;
pub const MIN_NOTIFY_DELIVERY_RETRY_SECONDS: i64 = 60;
pub const MAX_NOTIFY_DELIVERY_RETRY_SECONDS: i64 = 3600;

/// `NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS` (`handlers.py:delivery_retry_seconds`).
pub fn delivery_retry_seconds(env: &Env) -> i64 {
    const VAR: &str = "NYXMON_NOTIFY_DELIVERY_RETRY_SECONDS";
    let default = DEFAULT_NOTIFY_DELIVERY_RETRY_SECONDS;
    let value = env.stripped(VAR);
    if value.is_empty() {
        return default;
    }
    match parse_int(&value) {
        None => {
            env.warn_once(loggers::HANDLERS, VAR, &value, || {
                format!("{VAR} is invalid; using default {default}")
            });
            default
        }
        Some(0) => 0,
        Some(parsed)
            if !(MIN_NOTIFY_DELIVERY_RETRY_SECONDS..=MAX_NOTIFY_DELIVERY_RETRY_SECONDS)
                .contains(&parsed) =>
        {
            env.warn_once(loggers::HANDLERS, VAR, &value, || {
                format!(
                    "{VAR} must be 0 or between {MIN_NOTIFY_DELIVERY_RETRY_SECONDS} and \
                     {MAX_NOTIFY_DELIVERY_RETRY_SECONDS}; using default {default}"
                )
            });
            default
        }
        Some(parsed) => parsed,
    }
}

// ------------------------------------------------------------ site connectivity

/// `site_connectivity.py:SiteMode`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum SiteMode {
    Off,
    Observe,
    Enforce,
}

impl SiteMode {
    pub fn as_str(self) -> &'static str {
        match self {
            SiteMode::Off => "off",
            SiteMode::Observe => "observe",
            SiteMode::Enforce => "enforce",
        }
    }

    pub fn parse(value: &str) -> Option<SiteMode> {
        match value {
            "off" => Some(SiteMode::Off),
            "observe" => Some(SiteMode::Observe),
            "enforce" => Some(SiteMode::Enforce),
            _ => None,
        }
    }
}

/// What a probe target is.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ProbeKind {
    /// Open a TCP connection to an address literal.
    Tcp,
    /// Resolve a name.
    Dns,
}

impl ProbeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ProbeKind::Tcp => "tcp",
            ProbeKind::Dns => "dns",
        }
    }
}

/// `site_connectivity.py:ProbeTarget`.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct ProbeTarget {
    /// `ipv4`, `ipv6` or `dns`.
    pub path: &'static str,
    pub kind: ProbeKind,
    /// IP literal as configured (an IPv6 zone id is kept) or DNS name.
    pub host: String,
    /// TCP port, `0` for DNS targets.
    pub port: u16,
}

impl fmt::Display for ProbeTarget {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self.kind {
            ProbeKind::Dns => f.write_str(&self.host),
            ProbeKind::Tcp if self.host.contains(':') => write!(f, "[{}]:{}", self.host, self.port),
            ProbeKind::Tcp => write!(f, "{}:{}", self.host, self.port),
        }
    }
}

pub const ENV_SITE_MODE: &str = "NYXMON_SITE_CONNECTIVITY_MODE";
pub const ENV_SITE_PROBE_INTERVAL: &str = "NYXMON_SITE_PROBE_INTERVAL_SECONDS";
pub const ENV_SITE_PROBE_TIMEOUT: &str = "NYXMON_SITE_PROBE_TIMEOUT_SECONDS";
pub const ENV_SITE_IPV4_TARGETS: &str = "NYXMON_SITE_PROBE_IPV4_TARGETS";
pub const ENV_SITE_IPV6_TARGETS: &str = "NYXMON_SITE_PROBE_IPV6_TARGETS";
pub const ENV_SITE_DNS_NAMES: &str = "NYXMON_SITE_PROBE_DNS_NAMES";
pub const ENV_SITE_DOWN_AFTER_FAILURES: &str = "NYXMON_SITE_DOWN_AFTER_FAILURES";
pub const ENV_SITE_RECOVERY_GRACE: &str = "NYXMON_SITE_RECOVERY_GRACE_SECONDS";
pub const ENV_SITE_MAX_HOLD: &str = "NYXMON_SITE_MAX_HOLD_SECONDS";
pub const ENV_SITE_INCIDENT_NOTIFY_AFTER: &str = "NYXMON_SITE_INCIDENT_NOTIFY_AFTER_SECONDS";
pub const ENV_SITE_INCIDENT_REMINDER: &str = "NYXMON_SITE_INCIDENT_REMINDER_SECONDS";

pub const DEFAULT_SITE_IPV4_TARGETS: &str = "1.1.1.1:443,8.8.8.8:443,9.9.9.9:443";
pub const DEFAULT_SITE_IPV6_TARGETS: &str =
    "[2606:4700:4700::1111]:443,[2001:4860:4860::8888]:443,[2620:fe::fe]:443";
pub const DEFAULT_SITE_DNS_NAMES: &str = "cloudflare.com,google.com,quad9.net";

/// The environment part of `SiteConnectivityConfig` (`from_env`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SiteEnvConfig {
    pub mode: SiteMode,
    pub probe_interval: i64,
    pub probe_timeout: i64,
    /// Empty means the path is unobserved.
    pub ipv4_targets: Vec<ProbeTarget>,
    pub ipv6_targets: Vec<ProbeTarget>,
    pub dns_names: Vec<ProbeTarget>,
    pub down_after_failures: i64,
    pub recovery_grace_seconds: i64,
    pub max_hold_seconds: i64,
    pub incident_notify_after_seconds: i64,
    pub incident_reminder_seconds: i64,
}

impl Default for SiteEnvConfig {
    /// `SiteConnectivityConfig()`: the documented defaults.
    fn default() -> Self {
        SiteEnvConfig {
            mode: SiteMode::Off,
            probe_interval: 60,
            probe_timeout: 3,
            ipv4_targets: endpoints_from_value(DEFAULT_SITE_IPV4_TARGETS, 4, "ipv4")
                .unwrap_or_default(),
            ipv6_targets: endpoints_from_value(DEFAULT_SITE_IPV6_TARGETS, 6, "ipv6")
                .unwrap_or_default(),
            dns_names: dns_names_from_value(DEFAULT_SITE_DNS_NAMES).unwrap_or_default(),
            down_after_failures: 2,
            recovery_grace_seconds: 900,
            max_hold_seconds: 3 * 3600,
            incident_notify_after_seconds: 900,
            incident_reminder_seconds: 6 * 3600,
        }
    }
}

impl SiteEnvConfig {
    /// A snapshot older than three probe intervals is not trusted.
    pub fn stale_after_seconds(&self) -> i64 {
        3 * self.probe_interval
    }
}

/// `site_connectivity.py:_warn_env_once`; `default` is already a Python repr.
fn site_warn_once(env: &Env, var: &str, value: &str, reason: &str, default: &str) {
    env.warn_once(loggers::SITE_CONNECTIVITY, var, value, || {
        format!(
            "{var}={} is invalid ({reason}); using the default {default}",
            py_str_repr(value)
        )
    });
}

/// `site_connectivity.py:_bounded_env_int`.
fn site_bounded_int(env: &Env, var: &str, default: i64, low: i64, high: i64) -> i64 {
    let value = env.stripped(var);
    if value.is_empty() {
        return default;
    }
    match parse_int(&value) {
        None => {
            site_warn_once(env, var, &value, "not an integer", &default.to_string());
            default
        }
        Some(parsed) if parsed < low || parsed > high => {
            let reason = format!("must be between {low} and {high}");
            site_warn_once(env, var, &value, &reason, &default.to_string());
            default
        }
        Some(parsed) => parsed,
    }
}

/// `ipaddress.ip_address(host).version`, `None` when not an IP literal. An
/// IPv6 zone id (`%name`) is accepted like Python.
fn ip_version(host: &str) -> Option<u8> {
    if host.contains('/') {
        return None;
    }
    if host.parse::<Ipv4Addr>().is_ok() {
        return Some(4);
    }
    let (address, scope) = match host.split_once('%') {
        Some((address, scope)) => (address, Some(scope)),
        None => (host, None),
    };
    if let Some(scope) = scope
        && (scope.is_empty() || scope.contains('%'))
    {
        return None;
    }
    address.parse::<Ipv6Addr>().ok().map(|_| 6)
}

/// `site_connectivity.py:_split_endpoint`.
fn split_endpoint(token: &str) -> Option<(&str, &str)> {
    if let Some(rest) = token.strip_prefix('[') {
        let (host, rest) = rest.split_once(']')?;
        let port = rest.strip_prefix(':')?;
        return Some((host, port));
    }
    let (host, port) = token.split_once(':')?;
    if port.contains(':') {
        return None;
    }
    Some((host, port))
}

/// `site_connectivity.py:_endpoints_from_value`.
fn endpoints_from_value(value: &str, version: u8, path: &'static str) -> Option<Vec<ProbeTarget>> {
    let mut targets = Vec::new();
    for token in value.split(',') {
        let token = py_strip(token);
        if token.is_empty() {
            continue;
        }
        let (host, port_text) = split_endpoint(token)?;
        let found_version = ip_version(host)?;
        let port = parse_int(port_text)?;
        if found_version != version || !(1..=65535).contains(&port) {
            return None;
        }
        targets.push(ProbeTarget {
            path,
            kind: ProbeKind::Tcp,
            host: host.to_owned(),
            port: u16::try_from(port).ok()?,
        });
    }
    (!targets.is_empty()).then_some(targets)
}

/// `site_connectivity.py:_parse_endpoints`.
fn parse_endpoints(
    env: &Env,
    var: &str,
    default: &str,
    version: u8,
    path: &'static str,
) -> Vec<ProbeTarget> {
    let value = match env.raw(var) {
        None => default.to_owned(),
        Some(raw) => py_strip(&raw).to_owned(),
    };
    if value.is_empty() {
        return Vec::new();
    }
    match endpoints_from_value(&value, version, path) {
        Some(targets) => targets,
        None => {
            site_warn_once(
                env,
                var,
                &value,
                "not a list of ip:port endpoints",
                &py_str_repr(default),
            );
            endpoints_from_value(default, version, path).unwrap_or_default()
        }
    }
}

/// `site_connectivity.py:_dns_names_from_value`.
fn dns_names_from_value(value: &str) -> Option<Vec<ProbeTarget>> {
    let mut targets = Vec::new();
    for token in value.split(',') {
        let name = py_strip(token);
        if name.is_empty() {
            continue;
        }
        if name.chars().any(py_isspace) || name.contains('/') {
            return None;
        }
        targets.push(ProbeTarget {
            path: "dns",
            kind: ProbeKind::Dns,
            host: name.to_owned(),
            port: 0,
        });
    }
    (!targets.is_empty()).then_some(targets)
}

/// `site_connectivity.py:_parse_dns_names`.
fn parse_dns_names(env: &Env) -> Vec<ProbeTarget> {
    let value = match env.raw(ENV_SITE_DNS_NAMES) {
        None => DEFAULT_SITE_DNS_NAMES.to_owned(),
        Some(raw) => py_strip(&raw).to_owned(),
    };
    if value.is_empty() {
        return Vec::new();
    }
    match dns_names_from_value(&value) {
        Some(targets) => targets,
        None => {
            site_warn_once(
                env,
                ENV_SITE_DNS_NAMES,
                &value,
                "not a list of host names",
                &py_str_repr(DEFAULT_SITE_DNS_NAMES),
            );
            dns_names_from_value(DEFAULT_SITE_DNS_NAMES).unwrap_or_default()
        }
    }
}

/// `NYXMON_SITE_*` (`site_connectivity.py:SiteConnectivityConfig.from_env`).
pub fn site_env_config(env: &Env) -> SiteEnvConfig {
    let raw_mode = env.stripped(ENV_SITE_MODE).to_lowercase();
    let mode = if raw_mode.is_empty() {
        SiteMode::Off
    } else {
        SiteMode::parse(&raw_mode).unwrap_or_else(|| {
            site_warn_once(env, ENV_SITE_MODE, &raw_mode, "unknown mode", "'off'");
            SiteMode::Off
        })
    };
    let defaults = SiteEnvConfig::default();
    SiteEnvConfig {
        mode,
        probe_interval: site_bounded_int(
            env,
            ENV_SITE_PROBE_INTERVAL,
            defaults.probe_interval,
            15,
            600,
        ),
        probe_timeout: site_bounded_int(env, ENV_SITE_PROBE_TIMEOUT, defaults.probe_timeout, 1, 10),
        ipv4_targets: parse_endpoints(
            env,
            ENV_SITE_IPV4_TARGETS,
            DEFAULT_SITE_IPV4_TARGETS,
            4,
            "ipv4",
        ),
        ipv6_targets: parse_endpoints(
            env,
            ENV_SITE_IPV6_TARGETS,
            DEFAULT_SITE_IPV6_TARGETS,
            6,
            "ipv6",
        ),
        dns_names: parse_dns_names(env),
        down_after_failures: site_bounded_int(
            env,
            ENV_SITE_DOWN_AFTER_FAILURES,
            defaults.down_after_failures,
            1,
            10,
        ),
        recovery_grace_seconds: site_bounded_int(
            env,
            ENV_SITE_RECOVERY_GRACE,
            defaults.recovery_grace_seconds,
            60,
            3600,
        ),
        max_hold_seconds: site_bounded_int(
            env,
            ENV_SITE_MAX_HOLD,
            defaults.max_hold_seconds,
            600,
            86400,
        ),
        incident_notify_after_seconds: site_bounded_int(
            env,
            ENV_SITE_INCIDENT_NOTIFY_AFTER,
            defaults.incident_notify_after_seconds,
            60,
            86400,
        ),
        incident_reminder_seconds: site_bounded_int(
            env,
            ENV_SITE_INCIDENT_REMINDER,
            defaults.incident_reminder_seconds,
            60,
            2_592_000,
        ),
    }
}

// ------------------------------------------------------------------ notifier

/// The environment read by `AsyncTelegramNotifier.__init__`.
#[derive(Clone, Debug, PartialEq)]
pub struct NotifierEnv {
    /// `TELEGRAM_BOT_TOKEN` as set (not stripped; empty counts as missing).
    pub telegram_token: Option<String>,
    /// `TELEGRAM_CHAT_ID` as set (not stripped; empty counts as missing).
    pub telegram_chat_id: Option<String>,
    pub opsgate_submit_base_url: String,
    pub opsgate_submit_token: String,
    pub opsgate_approval_base_url: String,
    /// Exact, like Python's `int` (unbounded above).
    pub opsgate_ticket_expires_seconds: BigInt,
    /// `float()` of the parsed integer.
    pub opsgate_submit_timeout_seconds: f64,
    pub opsgate_include_warnings: bool,
}

impl NotifierEnv {
    /// `AsyncTelegramNotifier._opsgate_enabled`.
    pub fn opsgate_enabled(&self) -> bool {
        !self.opsgate_submit_base_url.is_empty() && !self.opsgate_submit_token.is_empty()
    }
}

/// `AsyncTelegramNotifier._parse_positive_int` (warns on every call).
fn notifier_positive_int(env: &Env, var: &str, default: i64) -> BigInt {
    let Some(raw) = env.raw(var) else {
        return BigInt::from(default);
    };
    let value = py_strip(&raw);
    if value.is_empty() {
        return BigInt::from(default);
    }
    match py_int(value).ok() {
        None => {
            env.warn(
                loggers::NOTIFICATION,
                &format!("{var} is not an integer, using default {default}"),
            );
            BigInt::from(default)
        }
        Some(parsed) if parsed <= BigInt::ZERO => {
            env.warn(
                loggers::NOTIFICATION,
                &format!("{var} must be > 0, using default {default}"),
            );
            BigInt::from(default)
        }
        Some(parsed) => parsed,
    }
}

/// `AsyncTelegramNotifier._parse_bool`.
fn notifier_bool(env: &Env, var: &str, default: bool) -> bool {
    let Some(raw) = env.raw(var) else {
        return default;
    };
    let normalized = py_strip(&raw).to_lowercase();
    if normalized.is_empty() {
        return default;
    }
    matches!(normalized.as_str(), "1" | "true" | "yes" | "on")
}

fn stripped_url(env: &Env, var: &str) -> String {
    env.stripped(var).trim_end_matches('/').to_owned()
}

/// `float(value)` for an `int`: correctly rounded, `OverflowError` beyond the
/// float range.
fn int_to_float(value: &BigInt) -> Result<f64, PyNumError> {
    let converted: f64 = value.to_string().parse().unwrap_or(f64::INFINITY);
    if converted.is_finite() {
        Ok(converted)
    } else {
        Err(PyNumError::OverflowError(
            "int too large to convert to float".to_owned(),
        ))
    }
}

/// `TELEGRAM_*` and `OPSGATE_*` (`notification.py:AsyncTelegramNotifier.__init__`).
///
/// # Errors
/// `OverflowError` when `OPSGATE_SUBMIT_TIMEOUT_SECONDS` is too large for a
/// float: Python's notifier constructor raises it, so the agent fails to
/// start.
pub fn notifier_env(env: &Env) -> Result<NotifierEnv, PyNumError> {
    let submit_base = stripped_url(env, "OPSGATE_SUBMIT_BASE_URL");
    let approval_base = stripped_url(env, "OPSGATE_APPROVAL_BASE_URL");
    let opsgate_ticket_expires_seconds =
        notifier_positive_int(env, "OPSGATE_TICKET_EXPIRES_SECONDS", 14400);
    let opsgate_submit_timeout_seconds = int_to_float(&notifier_positive_int(
        env,
        "OPSGATE_SUBMIT_TIMEOUT_SECONDS",
        10,
    ))?;
    Ok(NotifierEnv {
        telegram_token: env.raw("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id: env.raw("TELEGRAM_CHAT_ID"),
        opsgate_submit_token: env.stripped("OPSGATE_SUBMIT_TOKEN"),
        opsgate_approval_base_url: if approval_base.is_empty() {
            submit_base.clone()
        } else {
            approval_base
        },
        opsgate_submit_base_url: submit_base,
        opsgate_ticket_expires_seconds,
        opsgate_submit_timeout_seconds,
        opsgate_include_warnings: notifier_bool(env, "OPSGATE_SUBMIT_INCLUDE_WARNINGS", false),
    })
}

#[cfg(test)]
mod tests;
