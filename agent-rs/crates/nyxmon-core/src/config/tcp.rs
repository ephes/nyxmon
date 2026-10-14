//! TCP check configuration (`domain/tcp_config.py`) and the executor's
//! pre-I/O steps (`tcp_executor.py:TcpCheckExecutor.execute` up to
//! `_resolve_host`).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use num_traits::ToPrimitive;

use super::{ConfigOutcome, PyException};
use crate::pyjson::{PyJson, py_str, py_truthy};
use crate::pyurl::{py_ip_address, urlsplit_hostname};

/// The message of a check without a target host.
pub const HOST_REQUIRED: &str = "host is required (set check.url or data.host)";

/// `TcpCheckConfig.MAX_PORT`.
pub const MAX_PORT: i64 = 65535;

/// `tls_mode`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TlsMode {
    None,
    Implicit,
    Starttls,
}

impl TlsMode {
    pub fn as_str(self) -> &'static str {
        match self {
            TlsMode::None => "none",
            TlsMode::Implicit => "implicit",
            TlsMode::Starttls => "starttls",
        }
    }
}

/// `starttls_protocol`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StarttlsProtocol {
    Generic,
    Smtp,
    Imap,
    Sieve,
}

impl StarttlsProtocol {
    pub fn as_str(self) -> &'static str {
        match self {
            StarttlsProtocol::Generic => "generic",
            StarttlsProtocol::Smtp => "smtp",
            StarttlsProtocol::Imap => "imap",
            StarttlsProtocol::Sieve => "sieve",
        }
    }
}

/// `TcpCheckConfig` after `from_dict` + `validate`.
///
/// `host`, `sni` and `starttls_command` are not type-checked by Python and
/// keep their raw JSON value; the executor reproduces what Python does with
/// a non-string value.
#[derive(Clone, Debug, PartialEq)]
pub struct TcpConfig {
    pub port: i64,
    pub host: PyJson,
    pub tls_mode: TlsMode,
    pub connect_timeout: f64,
    pub tls_handshake_timeout: f64,
    pub retries: i64,
    pub retry_delay: f64,
    pub check_cert_expiry: bool,
    pub min_cert_days: i64,
    pub sni: PyJson,
    pub starttls_command: PyJson,
    pub starttls_protocol: StarttlsProtocol,
    pub starttls_read_greeting: bool,
    pub verify: bool,
}

/// `data.get(key, default)`.
fn get_or<'a>(data: &'a PyJson, key: &str, default: &'a PyJson) -> &'a PyJson {
    data.get(key).unwrap_or(default)
}

fn bounded_int(name: &str, value: &PyJson, minimum: i64, maximum: i64) -> Result<i64, String> {
    // `type(value) is not int or not minimum <= value <= maximum`
    match value {
        PyJson::Int(number) if (minimum..=maximum).contains(number) => Ok(*number),
        _ => Err(format!(
            "{name} must be an integer between {minimum} and {maximum}"
        )),
    }
}

fn duration(name: &str, value: &PyJson) -> Result<f64, String> {
    let allow_zero = name == "retry_delay";
    let seconds = match value {
        PyJson::Int(number) => Some(*number as f64),
        PyJson::Float(number) => Some(*number),
        // A big integer is never within 0..=300.
        _ => None,
    };
    match seconds {
        Some(seconds)
            if (0.0..=300.0).contains(&seconds)
                && seconds.is_finite()
                && (allow_zero || seconds != 0.0) =>
        {
            Ok(seconds)
        }
        _ => Err(format!(
            "{name} must be a finite number {}between 0 and 300 seconds",
            if allow_zero {
                ""
            } else {
                "greater than zero and "
            }
        )),
    }
}

/// Whether `"port" in data` raises (non-iterable) or is true, for a
/// non-dict `data` (PLAN.md D-11).
fn non_dict_has_port(data: &PyJson) -> Result<bool, PyException> {
    match data {
        PyJson::List(items) => Ok(items.iter().any(|item| item.as_str() == Some("port"))),
        PyJson::Str(text) => Ok(text.contains("port")),
        // `"port" in 5`: argument of type 'int' is not iterable.
        _ => Err(PyException::TYPE_ERROR),
    }
}

impl TcpConfig {
    /// `TcpCheckConfig.from_dict(check.data)` followed by `validate()`.
    pub fn from_data(data: &PyJson) -> ConfigOutcome<TcpConfig> {
        let empty;
        let data = if data.is_null() {
            empty = PyJson::dict();
            &empty
        } else {
            data
        };
        if data.as_dict().is_none() {
            return match non_dict_has_port(data) {
                // `data["port"]` on a list or str: TypeError.
                Ok(true) => ConfigOutcome::PyException(PyException::TYPE_ERROR),
                Ok(false) => ConfigOutcome::ConfigError("port is required".to_owned()),
                Err(exc) => ConfigOutcome::PyException(exc),
            };
        }
        let Some(port) = data.get("port") else {
            return ConfigOutcome::ConfigError("port is required".to_owned());
        };
        match Self::validate(data, port) {
            Ok(config) => ConfigOutcome::Ok(config),
            Err(Invalid::Message(message)) => ConfigOutcome::ConfigError(message),
            Err(Invalid::Exception(exc)) => ConfigOutcome::PyException(exc),
        }
    }

    fn validate(data: &PyJson, port: &PyJson) -> Result<TcpConfig, Invalid> {
        let null = PyJson::Null;
        let mut booleans = [false; 3];
        for (slot, (name, default)) in booleans.iter_mut().zip([
            ("verify", true),
            ("check_cert_expiry", false),
            ("starttls_read_greeting", false),
        ]) {
            *slot = match data.get(name) {
                None => default,
                Some(PyJson::Bool(flag)) => *flag,
                Some(_) => return Err(Invalid::Message(format!("{name} must be a boolean"))),
            };
        }
        let [verify, check_cert_expiry, starttls_read_greeting] = booleans;

        let default_retries = PyJson::Int(1);
        let default_min_days = PyJson::Int(14);
        let port = bounded_int("port", port, 1, MAX_PORT)?;
        let retries = bounded_int("retries", get_or(data, "retries", &default_retries), 0, 100)?;
        let min_cert_days = bounded_int(
            "min_cert_days",
            get_or(data, "min_cert_days", &default_min_days),
            0,
            36500,
        )?;

        let default_timeout = PyJson::Float(10.0);
        let default_delay = PyJson::Float(0.0);
        let connect_timeout = duration(
            "connect_timeout",
            get_or(data, "connect_timeout", &default_timeout),
        )?;
        let tls_handshake_timeout = duration(
            "tls_handshake_timeout",
            get_or(data, "tls_handshake_timeout", &default_timeout),
        )?;
        let retry_delay = duration("retry_delay", get_or(data, "retry_delay", &default_delay))?;

        let tls_mode = match data.get("tls_mode") {
            None => TlsMode::None,
            Some(PyJson::Str(mode)) if mode == "none" => TlsMode::None,
            Some(PyJson::Str(mode)) if mode == "implicit" => TlsMode::Implicit,
            Some(PyJson::Str(mode)) if mode == "starttls" => TlsMode::Starttls,
            // `mode not in ALLOWED_TLS_MODES` hashes the value: unhashable.
            Some(PyJson::List(_) | PyJson::Dict(_)) => {
                return Err(Invalid::Exception(PyException::TYPE_ERROR));
            }
            Some(_) => {
                return Err(Invalid::Message(
                    "tls_mode must be one of ['implicit', 'none', 'starttls']".to_owned(),
                ));
            }
        };

        // `data.get("starttls_protocol") or "generic"`.
        let protocol_value = data.get("starttls_protocol").unwrap_or(&null);
        let starttls_protocol = if !py_truthy(protocol_value) {
            StarttlsProtocol::Generic
        } else {
            match protocol_value.as_str() {
                Some("generic") => StarttlsProtocol::Generic,
                Some("smtp") => StarttlsProtocol::Smtp,
                Some("imap") => StarttlsProtocol::Imap,
                Some("sieve") => StarttlsProtocol::Sieve,
                _ => {
                    return Err(Invalid::Message(
                        "starttls_protocol must be one of ['generic', 'imap', 'sieve', 'smtp']"
                            .to_owned(),
                    ));
                }
            }
        };

        let starttls_command = data
            .get("starttls_command")
            .cloned()
            .unwrap_or_else(|| PyJson::Str("STARTTLS\r\n".to_owned()));
        if tls_mode == TlsMode::Starttls
            && starttls_protocol == StarttlsProtocol::Generic
            && !py_truthy(&starttls_command)
        {
            return Err(Invalid::Message(
                "starttls_command is required for starttls mode".to_owned(),
            ));
        }

        Ok(TcpConfig {
            port,
            host: data.get("host").cloned().unwrap_or(PyJson::Null),
            tls_mode,
            connect_timeout,
            tls_handshake_timeout,
            retries,
            retry_delay,
            check_cert_expiry,
            min_cert_days,
            sni: data.get("sni").cloned().unwrap_or(PyJson::Null),
            starttls_command,
            starttls_protocol,
            starttls_read_greeting,
            verify,
        })
    }

    /// `TcpCheckExecutor._resolve_host`: `data.host` when truthy, else the
    /// host of `check.url`, else the URL itself; `None` without a URL.
    ///
    /// # Errors
    /// `urlparse` raises `ValueError` for a malformed URL, which escapes the
    /// executor.
    pub fn target_host(&self, url: &str) -> Result<Option<PyJson>, PyException> {
        if py_truthy(&self.host) {
            return Ok(Some(self.host.clone()));
        }
        if url.is_empty() {
            return Ok(None);
        }
        let hostname = urlsplit_hostname(url).map_err(|_| PyException::VALUE_ERROR)?;
        Ok(Some(PyJson::Str(
            hostname.unwrap_or_else(|| url.to_owned()),
        )))
    }

    /// The number of attempts (`retries + 1`).
    pub fn attempts(&self) -> i64 {
        self.retries + 1
    }
}

enum Invalid {
    Message(String),
    Exception(PyException),
}

impl From<String> for Invalid {
    fn from(message: String) -> Self {
        Invalid::Message(message)
    }
}

/// Where `anyio.connect_tcp(host, port)` connects for a target host value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ConnectTarget {
    /// `ip_address(host)` succeeded: connect without resolving.
    Address(IpAddr),
    /// An IPv6 address with a `%zone`: connected through the resolver,
    /// which turns the zone into a scope id; a failure is a failed attempt.
    ScopedAddress(String),
    /// Resolve `str(host)` with `getaddrinfo`.
    Name(String),
}

/// `anyio.connect_tcp`'s view of the target host: `ip_address(remote_host)`
/// (which also accepts integers, and booleans as integers) or else the name
/// `str(remote_host)`.
pub fn connect_target(host: &PyJson) -> ConnectTarget {
    let from_int = |value: Option<u128>| {
        value.map(|n| match u32::try_from(n) {
            Ok(v4) => IpAddr::V4(Ipv4Addr::from(v4)),
            Err(_) => IpAddr::V6(Ipv6Addr::from(n)),
        })
    };
    let literal = match host {
        PyJson::Bool(flag) => from_int(Some(u128::from(*flag))),
        PyJson::Int(number) => from_int(u128::try_from(*number).ok()),
        PyJson::BigInt(number) => from_int(number.to_u128()),
        PyJson::Str(text) => match py_ip_address(text) {
            Some(ip) if ip.scope.is_some() => return ConnectTarget::ScopedAddress(text.clone()),
            Some(ip) => Some(ip.addr),
            None => None,
        },
        _ => None,
    };
    match literal {
        Some(addr) => ConnectTarget::Address(addr),
        None => ConnectTarget::Name(py_str(host)),
    }
}

#[cfg(test)]
mod tests;
