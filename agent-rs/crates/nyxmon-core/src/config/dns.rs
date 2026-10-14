//! DNS check configuration (`domain/dns_config.py`): `DnsCheckConfig`
//! `from_dict` + `validate`, `normalize_dns_value`, `expected_ip_error`, and
//! the executor's `_check_ip_match`. Pinned by `config_dns.json` and
//! `dns_text.json`.

use std::collections::HashSet;
use std::net::IpAddr;

use num_bigint::BigInt;
use num_traits::{ToPrimitive, Zero};

use super::sorted_set_repr;
use crate::pyjson::{CmpOp, PyJson, py_cmp, py_str, py_strip, py_truthy};
use crate::pyurl::{PyIpAddress, py_ip_address};

/// `VALID_QUERY_TYPES`.
pub const VALID_QUERY_TYPES: [&str; 8] = ["A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA", "PTR"];

/// A query type of `VALID_QUERY_TYPES`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum QueryType {
    A,
    Aaaa,
    Mx,
    Txt,
    Cname,
    Ns,
    Soa,
    Ptr,
}

impl QueryType {
    pub fn parse(text: &str) -> Option<QueryType> {
        Some(match text {
            "A" => QueryType::A,
            "AAAA" => QueryType::Aaaa,
            "MX" => QueryType::Mx,
            "TXT" => QueryType::Txt,
            "CNAME" => QueryType::Cname,
            "NS" => QueryType::Ns,
            "SOA" => QueryType::Soa,
            "PTR" => QueryType::Ptr,
            _ => return None,
        })
    }

    pub fn as_str(self) -> &'static str {
        match self {
            QueryType::A => "A",
            QueryType::Aaaa => "AAAA",
            QueryType::Mx => "MX",
            QueryType::Txt => "TXT",
            QueryType::Cname => "CNAME",
            QueryType::Ns => "NS",
            QueryType::Soa => "SOA",
            QueryType::Ptr => "PTR",
        }
    }

    /// The RR type code.
    pub fn code(self) -> u16 {
        match self {
            QueryType::A => 1,
            QueryType::Ns => 2,
            QueryType::Cname => 5,
            QueryType::Soa => 6,
            QueryType::Ptr => 12,
            QueryType::Mx => 15,
            QueryType::Txt => 16,
            QueryType::Aaaa => 28,
        }
    }

    /// `IP_QUERY_TYPE_VERSIONS.get(query_type)`.
    pub fn ip_version(self) -> Option<u8> {
        match self {
            QueryType::A => Some(4),
            QueryType::Aaaa => Some(6),
            _ => None,
        }
    }
}

/// A validated DNS check configuration.
#[derive(Debug)]
pub struct DnsConfig {
    pub expected_ips: Vec<String>,
    /// `data.get("dns_server")` as given; a truthy value is a `str` or an
    /// integer `ipaddress.ip_address` accepts.
    pub dns_server: PyJson,
    /// `data.get("source_ip")` as given (same rules as `dns_server`).
    pub source_ip: PyJson,
    pub query_type: QueryType,
    /// Seconds, `0 < timeout <= 300`.
    pub timeout: f64,
}

/// Why a configuration was rejected: the `ValueError` the executor reports
/// as `configuration_error`, or another exception it reports as
/// `unexpected_error` with `str(exc)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DnsConfigError {
    Value(String),
    Exception {
        class: &'static str,
        message: String,
    },
}

impl DnsConfigError {
    fn value(message: impl Into<String>) -> Self {
        DnsConfigError::Value(message.into())
    }

    fn type_error(message: impl Into<String>) -> Self {
        DnsConfigError::Exception {
            class: "TypeError",
            message: message.into(),
        }
    }
}

/// Python's type name of a JSON value in `TypeError` messages.
fn type_name(value: &PyJson) -> &'static str {
    crate::pyjson::py_type_name(value)
}

/// `"expected_ips" in data` followed by `data["expected_ips"]` for any
/// `check.data` value (PLAN.md D-11).
fn expected_ips_value(data: &PyJson) -> Result<&PyJson, DnsConfigError> {
    const KEY: &str = "expected_ips";
    let required = || DnsConfigError::value("expected_ips is required");
    match data {
        PyJson::Dict(map) => map.get(KEY).ok_or_else(required),
        PyJson::List(items) => {
            let key = PyJson::from(KEY);
            if items
                .iter()
                .any(|item| py_cmp(CmpOp::Eq, item, &key).unwrap_or(false))
            {
                Err(DnsConfigError::type_error(
                    "list indices must be integers or slices, not str",
                ))
            } else {
                Err(required())
            }
        }
        PyJson::Str(text) => {
            if text.contains(KEY) {
                Err(DnsConfigError::type_error(
                    "string indices must be integers, not 'str'",
                ))
            } else {
                Err(required())
            }
        }
        other => Err(DnsConfigError::type_error(format!(
            "argument of type '{}' is not iterable",
            type_name(other)
        ))),
    }
}

/// `ipaddress.ip_address(value)` for a JSON value: a `str` is parsed, an
/// integer (a bool is an int) selects an IPv4 (`< 2**32`) or IPv6
/// (`< 2**128`) address; everything else is `str(value)` parsed, which
/// never yields an address for floats, lists and dicts.
pub fn ip_address_of(value: &PyJson) -> Option<PyIpAddress> {
    let int_address = |number: BigInt| -> Option<PyIpAddress> {
        if number < BigInt::zero() {
            return None;
        }
        let addr = if let Some(v4) = number.to_u32() {
            IpAddr::from(v4.to_be_bytes())
        } else {
            IpAddr::from(number.to_u128()?.to_be_bytes())
        };
        Some(PyIpAddress { addr, scope: None })
    };
    match value {
        PyJson::Str(text) => py_ip_address(text),
        PyJson::Bool(flag) => int_address(BigInt::from(u8::from(*flag))),
        PyJson::Int(number) => int_address(BigInt::from(*number)),
        PyJson::BigInt(number) => int_address(number.clone()),
        _ => None,
    }
}

impl DnsConfig {
    /// `DnsCheckConfig.from_dict(data)` followed by `validate()`.
    ///
    /// # Errors
    /// The Python exception, see [`DnsConfigError`].
    pub fn from_data(data: &PyJson) -> Result<DnsConfig, DnsConfigError> {
        let expected = expected_ips_value(data)?;
        let expected_ips: Vec<String> = match expected.as_list() {
            Some(items) if items.iter().all(|item| item.as_str().is_some()) => items
                .iter()
                .map(|item| item.as_str().unwrap_or_default().to_owned())
                .collect(),
            _ => {
                return Err(DnsConfigError::value(
                    "expected_ips must be a list of strings",
                ));
            }
        };
        if expected_ips.is_empty() {
            return Err(DnsConfigError::value("expected_ips cannot be empty"));
        }
        let map = data.as_dict().expect("only a dict holds expected_ips");
        let query_type_value = map.get("query_type").cloned().unwrap_or(PyJson::from("A"));
        let timeout_value = map.get("timeout").cloned().unwrap_or(PyJson::Float(5.0));
        let dns_server = map.get("dns_server").cloned().unwrap_or(PyJson::Null);
        let source_ip = map.get("source_ip").cloned().unwrap_or(PyJson::Null);

        // validate()
        let query_type = match &query_type_value {
            PyJson::List(_) | PyJson::Dict(_) => {
                return Err(DnsConfigError::type_error(format!(
                    "unhashable type: '{}'",
                    type_name(&query_type_value)
                )));
            }
            PyJson::Str(text) => QueryType::parse(text),
            _ => None,
        };
        let Some(query_type) = query_type else {
            return Err(DnsConfigError::value(format!(
                "Invalid query_type: {}. Must be one of {}",
                py_str(&query_type_value),
                sorted_set_repr(&VALID_QUERY_TYPES)
            )));
        };
        let timeout = match &timeout_value {
            PyJson::Int(number) if (1..=300).contains(number) => *number as f64,
            PyJson::Float(number) if *number > 0.0 && *number <= 300.0 => *number,
            _ => {
                return Err(DnsConfigError::value(
                    "timeout must be a finite positive number up to 300 seconds",
                ));
            }
        };
        for ip in &expected_ips {
            if let Some(error) = expected_ip_error(ip, query_type) {
                return Err(DnsConfigError::value(format!(
                    "Invalid expected_ips entry: {error}"
                )));
            }
        }
        for (name, value) in [("source_ip", &source_ip), ("dns_server", &dns_server)] {
            if py_truthy(value) && ip_address_of(value).is_none() {
                return Err(DnsConfigError::value(format!(
                    "Invalid {name}: {}. Must be a valid IP address.",
                    py_str(value)
                )));
            }
        }
        Ok(DnsConfig {
            expected_ips,
            dns_server,
            source_ip,
            query_type,
            timeout,
        })
    }
}

/// `str(ipaddress.ip_address(...))`: dotted quad; IPv6 hextets in lowercase
/// hex without leading zeros, the longest run (the first on a tie) of at
/// least two zero hextets as `::`, IPv4-mapped addresses as
/// `::ffff:a.b.c.d`; the zone appended after `%`.
pub fn py_ip_text(address: &PyIpAddress) -> String {
    let text = match address.addr {
        IpAddr::V4(v4) => v4.to_string(),
        IpAddr::V6(v6) => {
            let octets = v6.octets();
            if octets[..10].iter().all(|b| *b == 0) && octets[10] == 0xff && octets[11] == 0xff {
                format!(
                    "::ffff:{}.{}.{}.{}",
                    octets[12], octets[13], octets[14], octets[15]
                )
            } else {
                compress_hextets(&v6.segments())
            }
        }
    };
    match &address.scope {
        Some(scope) => format!("{text}%{scope}"),
        None => text,
    }
}

/// `_compress_hextets` over the eight hextets.
fn compress_hextets(segments: &[u16; 8]) -> String {
    let (mut best_start, mut best_len) = (0usize, 0usize);
    let (mut start, mut len) = (0usize, 0usize);
    for (index, segment) in segments.iter().enumerate() {
        if *segment == 0 {
            if len == 0 {
                start = index;
            }
            len += 1;
            if len > best_len {
                best_len = len;
                best_start = start;
            }
        } else {
            len = 0;
        }
    }
    let hex = |range: &[u16]| {
        range
            .iter()
            .map(|s| format!("{s:x}"))
            .collect::<Vec<_>>()
            .join(":")
    };
    if best_len > 1 {
        format!(
            "{}::{}",
            hex(&segments[..best_start]),
            hex(&segments[best_start + best_len..])
        )
    } else {
        hex(segments)
    }
}

/// `normalize_dns_value`: the canonical text of an IP address (after
/// `strip()`), any other value unchanged.
pub fn normalize_dns_value(value: &str) -> String {
    match py_ip_address(py_strip(value)) {
        Some(address) => py_ip_text(&address),
        None => value.to_owned(),
    }
}

/// `expected_ip_error(value, query_type)`.
pub fn expected_ip_error(value: &str, query_type: QueryType) -> Option<String> {
    let version = query_type.ip_version()?;
    let Some(address) = py_ip_address(py_strip(value)) else {
        return Some(format!("Invalid IP address: {value}"));
    };
    let actual = if address.addr.is_ipv4() { 4 } else { 6 };
    (actual != version).then(|| {
        format!(
            "{value} is an IPv{actual} address, but a {} query only returns IPv{version} addresses",
            query_type.as_str()
        )
    })
}

/// `DnsCheckExecutor._check_ip_match`: any common value, compared as IP
/// addresses (normalised) for A/AAAA queries and exactly otherwise.
pub fn check_ip_match(resolved: &[String], expected: &[String], compare_addresses: bool) -> bool {
    let set = |values: &[String]| -> HashSet<String> {
        values
            .iter()
            .map(|value| {
                if compare_addresses {
                    normalize_dns_value(value)
                } else {
                    value.clone()
                }
            })
            .collect()
    };
    let resolved = set(resolved);
    set(expected).iter().any(|value| resolved.contains(value))
}

#[cfg(test)]
mod tests;
