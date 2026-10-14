//! `urllib.parse.urlsplit(url).hostname` and `ipaddress.ip_address(text)`
//! as CPython 3.13 implements them (used by `tcp_executor.py:_resolve_host`
//! and `ping_config.py:normalize_ping_target`).

use std::fmt;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use crate::pyjson::{nfkc_adds_url_delimiter, py_lower, py_str_repr};

/// The `ValueError` `urlsplit` raises for a malformed network location.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UrlValueError(pub String);

impl fmt::Display for UrlValueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for UrlValueError {}

/// An address `ipaddress.ip_address(text)` accepts.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PyIpAddress {
    pub addr: IpAddr,
    /// The IPv6 zone after `%` (non-empty, without `%`).
    pub scope: Option<String>,
}

/// `ipaddress.ip_address(text)` for a `str`: a strict dotted-quad IPv4
/// address, or an IPv6 address with an optional `%zone`.
pub fn py_ip_address(text: &str) -> Option<PyIpAddress> {
    if let Ok(v4) = text.parse::<Ipv4Addr>() {
        return Some(PyIpAddress {
            addr: IpAddr::V4(v4),
            scope: None,
        });
    }
    let (address, scope) = match text.split_once('%') {
        None => (text, None),
        Some((_, "")) => return None,
        Some((_, zone)) if zone.contains('%') => return None,
        Some((address, zone)) => (address, Some(zone.to_owned())),
    };
    address.parse::<Ipv6Addr>().ok().map(|v6| PyIpAddress {
        addr: IpAddr::V6(v6),
        scope,
    })
}

/// `scheme_chars`.
fn is_scheme_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')
}

fn invalid_ipv6() -> UrlValueError {
    UrlValueError("Invalid IPv6 URL".to_owned())
}

/// `_check_bracketed_host`.
fn check_bracketed_host(hostname: &str) -> Result<(), UrlValueError> {
    if let Some(rest) = hostname
        .strip_prefix('v')
        .or_else(|| hostname.strip_prefix('V'))
    {
        // `\A[vV][a-fA-F0-9]+\..+\Z` (`.` does not match a newline).
        let hex_len = rest.chars().take_while(char::is_ascii_hexdigit).count();
        let after = &rest[hex_len..];
        let valid = hex_len > 0
            && after
                .strip_prefix('.')
                .is_some_and(|tail| !tail.is_empty() && !tail.contains('\n'));
        return if valid {
            Ok(())
        } else {
            Err(UrlValueError("IPvFuture address is invalid".to_owned()))
        };
    }
    match py_ip_address(hostname) {
        None => Err(UrlValueError(format!(
            "{} does not appear to be an IPv4 or IPv6 address",
            py_str_repr(hostname)
        ))),
        Some(PyIpAddress {
            addr: IpAddr::V4(_),
            ..
        }) => Err(UrlValueError(
            "An IPv4 address cannot be in brackets".to_owned(),
        )),
        Some(_) => Ok(()),
    }
}

/// `_check_bracketed_netloc`.
fn check_bracketed_netloc(netloc: &str) -> Result<(), UrlValueError> {
    let host_and_port = rpartition_after(netloc, '@');
    let hostname = match host_and_port.split_once('[') {
        Some((before, bracketed)) => {
            if !before.is_empty() {
                return Err(invalid_ipv6());
            }
            let (hostname, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            if !port.is_empty() && !port.starts_with(':') {
                return Err(invalid_ipv6());
            }
            hostname
        }
        None => host_and_port
            .split_once(':')
            .map_or(host_and_port, |(h, _)| h),
    };
    check_bracketed_host(hostname)
}

/// `_checknetloc`: a non-ASCII netloc must not gain URL delimiters under
/// NFKC normalisation. With `@:#?` removed first, the normalised text gains
/// a delimiter exactly when one of its characters' NFKC forms holds one.
fn check_netloc(netloc: &str) -> Result<(), UrlValueError> {
    if netloc.is_ascii() {
        return Ok(());
    }
    let gains_delimiter = netloc
        .chars()
        .filter(|c| !matches!(c, '@' | ':' | '#' | '?'))
        .any(nfkc_adds_url_delimiter);
    if gains_delimiter {
        return Err(UrlValueError(format!(
            "netloc '{netloc}' contains invalid characters under NFKC normalization"
        )));
    }
    Ok(())
}

/// `text.rpartition(sep)[2]`.
fn rpartition_after(text: &str, sep: char) -> &str {
    text.rsplit_once(sep).map_or(text, |(_, after)| after)
}

/// The netloc of `urlsplit(url)`.
///
/// # Errors
/// The `ValueError`s of `urlsplit` (unbalanced or invalid brackets, NFKC
/// delimiters).
pub fn urlsplit_netloc(url: &str) -> Result<String, UrlValueError> {
    // `url.lstrip(_WHATWG_C0_CONTROL_OR_SPACE)`, then remove tab/CR/LF.
    let url: String = url
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = url.as_str();
    if let Some(colon) = rest.find(':')
        && colon > 0
        && rest.as_bytes()[0].is_ascii_alphabetic()
        && rest[..colon].chars().all(is_scheme_char)
    {
        rest = &rest[colon + 1..];
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = &after[..end];
        let open = netloc.contains('[');
        let close = netloc.contains(']');
        if open != close {
            return Err(invalid_ipv6());
        }
        if open && close {
            check_bracketed_netloc(netloc)?;
        }
    }
    check_netloc(netloc)?;
    Ok(netloc.to_owned())
}

/// `urlsplit(url).hostname`: the host of the network location, lowercased
/// except for an IPv6 zone, or `None` when it is empty.
///
/// # Errors
/// See [`urlsplit_netloc`].
pub fn urlsplit_hostname(url: &str) -> Result<Option<String>, UrlValueError> {
    let netloc = urlsplit_netloc(url)?;
    let host_info = rpartition_after(&netloc, '@');
    let hostname = match host_info.split_once('[') {
        Some((_, bracketed)) => bracketed.split_once(']').map_or(bracketed, |(h, _)| h),
        None => host_info.split_once(':').map_or(host_info, |(h, _)| h),
    };
    if hostname.is_empty() {
        return Ok(None);
    }
    Ok(Some(match hostname.split_once('%') {
        Some((host, zone)) => format!("{}%{zone}", py_lower(host)),
        None => py_lower(hostname),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hostnames() {
        let cases: &[(&str, Option<&str>)] = &[
            ("https://Example.COM:8443/x", Some("example.com")),
            ("example.com", None),
            ("//h/x", Some("h")),
            ("https://u:p@H:1/", Some("h")),
            ("https://[FE80::1%25En0]/", Some("fe80::1%25En0")),
            ("https://:80/", None),
        ];
        for (url, expected) in cases {
            assert_eq!(
                urlsplit_hostname(url).unwrap().as_deref(),
                *expected,
                "{url}"
            );
        }
        assert!(urlsplit_hostname("https://[::1").is_err());
        assert!(urlsplit_hostname("https://[1.2.3.4]/").is_err());
        assert!(urlsplit_hostname("https://h\u{2100}st/").is_err());
    }

    #[test]
    fn ip_addresses() {
        assert!(py_ip_address("192.0.2.1").is_some());
        assert!(py_ip_address("01.2.3.4").is_none());
        assert_eq!(
            py_ip_address("fe80::1%en0").unwrap().scope.as_deref(),
            Some("en0")
        );
        assert!(py_ip_address("fe80::1%").is_none());
        assert!(py_ip_address("fe80::1%a%b").is_none());
        assert!(py_ip_address("1.2.3.4%x").is_none());
    }
}
