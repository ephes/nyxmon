//! httpx 0.28 URL semantics (`httpx/_urlparse.py`, `httpx/_urls.py`) and
//! the `urllib.parse.urljoin` that `URL.join` uses, pinned by
//! `fixtures/httpx_url.json`.
//!
//! The request target the agent sends is httpx's `raw_path`: the path is
//! percent-quoted but otherwise kept as written (only literal `.`/`..`
//! segments are removed; `%2e%2e` is sent as is), unlike a WHATWG URL parser.

use std::fmt;
use std::net::{Ipv4Addr, Ipv6Addr};

use nyxmon_core::pyjson::py_int;

/// `httpx.InvalidURL` (not a `RequestError`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvalidUrl(pub String);

impl fmt::Display for InvalidUrl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for InvalidUrl {}

const MAX_URL_LENGTH: usize = 65536;
const UNRESERVED: &str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
const SUB_DELIMS: &str = "!$&'()*+,;=";

/// The printable ASCII range minus `excluded` (the `*_SAFE` tables).
fn safe_set(excluded: &[u8]) -> Vec<u8> {
    (0x20u8..0x7f).filter(|b| !excluded.contains(b)).collect()
}

fn frag_safe() -> Vec<u8> {
    safe_set(&[0x20, 0x22, 0x3c, 0x3e, 0x60])
}

fn query_safe() -> Vec<u8> {
    safe_set(&[0x20, 0x22, 0x23, 0x3c, 0x3e])
}

fn path_safe() -> Vec<u8> {
    safe_set(&[0x20, 0x22, 0x23, 0x3c, 0x3e, 0x3f, 0x60, 0x7b, 0x7d])
}

fn userinfo_safe() -> Vec<u8> {
    safe_set(&[
        0x20, 0x22, 0x23, 0x3c, 0x3e, 0x3f, 0x60, 0x7b, 0x7d, 0x2f, 0x3b, 0x3d, 0x40, 0x5b, 0x5c,
        0x5d, 0x5e, 0x7c,
    ])
}

/// `percent_encoded(string, safe)`.
fn percent_encoded(text: &str, safe: &[u8]) -> String {
    let keep = |c: char| c.is_ascii() && (UNRESERVED.contains(c) || safe.contains(&(c as u8)));
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if keep(c) {
            out.push(c);
        } else {
            let mut buf = [0u8; 4];
            for byte in c.encode_utf8(&mut buf).bytes() {
                out.push_str(&format!("%{byte:02X}"));
            }
        }
    }
    out
}

fn is_percent_escape(bytes: &[u8]) -> bool {
    bytes.len() >= 3
        && bytes[0] == b'%'
        && bytes[1].is_ascii_hexdigit()
        && bytes[2].is_ascii_hexdigit()
}

/// `quote(string, safe)`: percent-encode, keeping existing `%xx` escapes.
fn quote(text: &str, safe: &[u8]) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut start = 0;
    let mut i = 0;
    while i < bytes.len() {
        if is_percent_escape(&bytes[i..]) {
            out.push_str(&percent_encoded(&text[start..i], safe));
            out.push_str(&text[i..i + 3]);
            i += 3;
            start = i;
        } else {
            i += 1;
        }
    }
    out.push_str(&percent_encoded(&text[start..], safe));
    out
}

/// `urllib.parse.unquote` (UTF-8, invalid sequences replaced).
fn unquote(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if is_percent_escape(&bytes[i..]) {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).expect("ascii hex");
            out.push(u8::from_str_radix(hex, 16).expect("hex"));
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Python's `ipaddress.IPv4Address(text)` acceptance.
fn valid_ipv4(text: &str) -> bool {
    let octets: Vec<&str> = text.split('.').collect();
    octets.len() == 4
        && octets.iter().all(|o| {
            !o.is_empty()
                && o.len() <= 3
                && o.bytes().all(|b| b.is_ascii_digit())
                && !(o.len() > 1 && o.starts_with('0'))
                && o.parse::<u16>().is_ok_and(|n| n <= 255)
        })
}

fn encode_host(host: &str) -> Result<String, InvalidUrl> {
    if host.is_empty() {
        return Ok(String::new());
    }
    let ipv4_style = {
        let parts: Vec<&str> = host.split('.').collect();
        parts.len() == 4
            && parts
                .iter()
                .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
    };
    if ipv4_style {
        if !valid_ipv4(host) || host.parse::<Ipv4Addr>().is_err() {
            return Err(InvalidUrl(format!("Invalid IPv4 address: '{host}'")));
        }
        return Ok(host.to_owned());
    }
    if host.starts_with('[') && host.ends_with(']') && host.len() >= 2 {
        let inner = &host[1..host.len() - 1];
        // ipaddress.IPv6Address: an optional non-empty scope without '%'.
        let valid = match inner.split_once('%') {
            Some((address, scope)) => {
                !scope.is_empty() && !scope.contains('%') && address.parse::<Ipv6Addr>().is_ok()
            }
            None => inner.parse::<Ipv6Addr>().is_ok(),
        };
        if !valid {
            return Err(InvalidUrl(format!("Invalid IPv6 address: '{host}'")));
        }
        return Ok(inner.to_owned());
    }
    if host.is_ascii() {
        let mut safe: Vec<u8> = SUB_DELIMS.bytes().collect();
        safe.extend_from_slice(b"\"`{}%|\\");
        return Ok(quote(&host.to_lowercase(), &safe));
    }
    crate::idna2008::idna_encode(&host.to_lowercase())
        .map_err(|_| InvalidUrl(format!("Invalid IDNA hostname: '{host}'")))
}

fn default_port(scheme: &str) -> Option<i64> {
    match scheme {
        "ftp" => Some(21),
        "http" | "ws" => Some(80),
        "https" | "wss" => Some(443),
        _ => None,
    }
}

/// `normalize_path`: drop literal `.` and `..` segments.
fn normalize_path(path: &str) -> String {
    if !path.contains('.') {
        return path.to_owned();
    }
    let components: Vec<&str> = path.split('/').collect();
    if !components.iter().any(|c| *c == "." || *c == "..") {
        return path.to_owned();
    }
    let mut output: Vec<&str> = Vec::new();
    for component in components {
        match component {
            "." => {}
            ".." => {
                if !(output.is_empty() || output == [""]) {
                    output.pop();
                }
            }
            other => output.push(other),
        }
    }
    output.join("/")
}

/// A parsed httpx URL (`ParseResult`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HttpxUrl {
    pub scheme: String,
    pub userinfo: String,
    /// Encoded host without IPv6 brackets.
    pub host: String,
    /// `None` when absent or the scheme's default.
    pub port: Option<i64>,
    pub path: String,
    pub query: Option<String>,
    pub fragment: Option<String>,
}

fn scheme_len(url: &str) -> Option<usize> {
    let bytes = url.as_bytes();
    if !bytes.first().is_some_and(u8::is_ascii_alphabetic) {
        return None;
    }
    let end = bytes
        .iter()
        .position(|b| !(b.is_ascii_alphanumeric() || matches!(b, b'+' | b'.' | b'-')))
        .unwrap_or(bytes.len());
    (bytes.get(end) == Some(&b':')).then_some(end)
}

impl HttpxUrl {
    /// `urlparse(url)`.
    ///
    /// # Errors
    /// Every input httpx rejects with `InvalidURL`.
    pub fn parse(url: &str) -> Result<HttpxUrl, InvalidUrl> {
        if url.chars().count() > MAX_URL_LENGTH {
            return Err(InvalidUrl("URL too long".to_owned()));
        }
        if let Some((idx, c)) = url
            .chars()
            .enumerate()
            .find(|(_, c)| c.is_ascii() && (c.is_ascii_control()))
        {
            return Err(InvalidUrl(format!(
                "Invalid non-printable ASCII character in URL, {:?} at position {idx}.",
                c
            )));
        }
        // URL_REGEX
        let mut rest = url;
        let mut scheme = "";
        if let Some(end) = scheme_len(rest) {
            scheme = &rest[..end];
            rest = &rest[end + 1..];
        } else if let Some(stripped) = rest.strip_prefix(':') {
            rest = stripped;
        }
        let mut authority = "";
        if let Some(after) = rest.strip_prefix("//") {
            let end = after.find(['/', '?', '#']).unwrap_or(after.len());
            authority = &after[..end];
            rest = &after[end..];
        }
        let path_end = rest.find(['?', '#']).unwrap_or(rest.len());
        let path = &rest[..path_end];
        rest = &rest[path_end..];
        let mut query = None;
        if let Some(after) = rest.strip_prefix('?') {
            let end = after.find('#').unwrap_or(after.len());
            query = Some(&after[..end]);
            rest = &after[end..];
        }
        let fragment = rest.strip_prefix('#');

        // AUTHORITY_REGEX
        let (userinfo, hostport) = match authority.rfind('@') {
            Some(at) => (&authority[..at], &authority[at + 1..]),
            None => ("", authority),
        };
        let (host, port) = if hostport.starts_with('[') && hostport.contains(']') {
            let end = hostport.rfind(']').expect("contains ]");
            let tail = &hostport[end + 1..];
            (&hostport[..=end], tail.strip_prefix(':').unwrap_or(tail))
        } else {
            let end = hostport.find(':').unwrap_or(hostport.len());
            let tail = &hostport[end..];
            (&hostport[..end], tail.strip_prefix(':').unwrap_or(tail))
        };

        let scheme = scheme.to_ascii_lowercase();
        let userinfo = quote(userinfo, &userinfo_safe());
        let host = encode_host(host)?;
        let port = if port.is_empty() {
            None
        } else {
            let value = py_int(port).map_err(|_| InvalidUrl(format!("Invalid port: '{port}'")))?;
            let value = nyxmon_core::pyjson::bigint_to_i64_saturating(&value);
            (default_port(&scheme) != Some(value)).then_some(value)
        };
        let has_scheme = !scheme.is_empty();
        let has_authority = !userinfo.is_empty() || !host.is_empty() || port.is_some();
        if has_authority && !path.is_empty() && !path.starts_with('/') {
            return Err(InvalidUrl(
                "For absolute URLs, path must be empty or begin with '/'".to_owned(),
            ));
        }
        if !has_scheme && !has_authority {
            if path.starts_with("//") {
                return Err(InvalidUrl(
                    "Relative URLs cannot have a path starting with '//'".to_owned(),
                ));
            }
            if path.starts_with(':') {
                return Err(InvalidUrl(
                    "Relative URLs cannot have a path starting with ':'".to_owned(),
                ));
            }
        }
        let path = if has_scheme || has_authority {
            normalize_path(path)
        } else {
            path.to_owned()
        };
        Ok(HttpxUrl {
            scheme,
            userinfo,
            host,
            port,
            path: quote(&path, &path_safe()),
            query: query.map(|q| quote(q, &query_safe())),
            fragment: fragment.map(|f| quote(f, &frag_safe())),
        })
    }

    fn bracketed_host(&self) -> String {
        if self.host.contains(':') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        }
    }

    /// `ParseResult.netloc`, the `Host` header value.
    pub fn netloc(&self) -> String {
        match self.port {
            Some(port) => format!("{}:{port}", self.bracketed_host()),
            None => self.bracketed_host(),
        }
    }

    fn authority(&self) -> String {
        let userinfo = if self.userinfo.is_empty() {
            String::new()
        } else {
            format!("{}@", self.userinfo)
        };
        format!("{userinfo}{}", self.netloc())
    }

    /// `URL.raw_path`: the request target.
    pub fn raw_path(&self) -> String {
        let path = if self.path.is_empty() {
            "/"
        } else {
            &self.path
        };
        match &self.query {
            Some(query) => format!("{path}?{query}"),
            None => path.to_owned(),
        }
    }

    /// `URL.is_relative_url`.
    pub fn is_relative(&self) -> bool {
        self.scheme.is_empty() || self.host.is_empty()
    }

    pub fn username(&self) -> String {
        unquote(
            self.userinfo
                .split_once(':')
                .map_or(&self.userinfo, |(u, _)| u),
        )
    }

    pub fn password(&self) -> String {
        unquote(self.userinfo.split_once(':').map_or("", |(_, p)| p))
    }

    /// The port to connect to.
    pub fn port_or_default(&self) -> Option<i64> {
        self.port.or_else(|| default_port(&self.scheme))
    }

    /// `URL.copy_with(host=...)`.
    ///
    /// # Errors
    /// The re-parsed URL is invalid.
    pub fn with_host(&self, host: &str) -> Result<HttpxUrl, InvalidUrl> {
        if !host.is_empty() && !self.path.is_empty() && !self.path.starts_with('/') {
            return Err(InvalidUrl(
                "For absolute URLs, path must be empty or begin with '/'".to_owned(),
            ));
        }
        let mut copy = self.clone();
        copy.host = host.to_owned();
        HttpxUrl::parse(&copy.to_string())
    }

    /// `URL.join(url)`: `urljoin(str(self), str(URL(url)))`, re-parsed.
    ///
    /// # Errors
    /// The joined URL is invalid.
    pub fn join(&self, other: &HttpxUrl) -> Result<HttpxUrl, InvalidUrl> {
        HttpxUrl::parse(&urljoin(&self.to_string(), &other.to_string()))
    }
}

impl fmt::Display for HttpxUrl {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if !self.scheme.is_empty() {
            write!(f, "{}:", self.scheme)?;
        }
        let authority = self.authority();
        if !authority.is_empty() {
            write!(f, "//{authority}")?;
        }
        f.write_str(&self.path)?;
        if let Some(query) = &self.query {
            write!(f, "?{query}")?;
        }
        if let Some(fragment) = &self.fragment {
            write!(f, "#{fragment}")?;
        }
        Ok(())
    }
}

// ------------------------------------------------- urllib.parse.urljoin

const USES_RELATIVE: &[&str] = &[
    "", "ftp", "http", "gopher", "nntp", "imap", "wais", "file", "https", "shttp", "mms",
    "prospero", "rtsp", "rtsps", "rtspu", "sftp", "svn", "svn+ssh", "ws", "wss",
];
const USES_NETLOC: &[&str] = &[
    "",
    "ftp",
    "http",
    "gopher",
    "nntp",
    "telnet",
    "imap",
    "wais",
    "file",
    "mms",
    "https",
    "shttp",
    "snews",
    "prospero",
    "rtsp",
    "rtsps",
    "rtspu",
    "rsync",
    "svn",
    "svn+ssh",
    "sftp",
    "nfs",
    "git",
    "git+ssh",
    "ws",
    "wss",
    "itms-services",
];
const USES_PARAMS: &[&str] = &[
    "", "ftp", "hdl", "prospero", "http", "imap", "https", "shttp", "rtsp", "rtsps", "rtspu",
    "sip", "sips", "mms", "sftp", "tel",
];

#[derive(Default)]
struct Parts {
    scheme: String,
    netloc: String,
    path: String,
    params: String,
    query: String,
    fragment: String,
}

/// `urllib.parse.urlparse(url, default_scheme)` for already-quoted URLs.
fn urlparse(url: &str, default_scheme: &str) -> Parts {
    let mut rest = url;
    let mut scheme = default_scheme.to_owned();
    if let Some(i) = rest.find(':')
        && i > 0
        && rest.as_bytes()[0].is_ascii_alphabetic()
        && rest[..i]
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'))
    {
        scheme = rest[..i].to_ascii_lowercase();
        rest = &rest[i + 1..];
    }
    let mut netloc = String::new();
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = after[..end].to_owned();
        rest = &after[end..];
    }
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (mut path, query) = rest.split_once('?').unwrap_or((rest, ""));
    let mut params = "";
    if USES_PARAMS.contains(&scheme.as_str()) && path.contains(';') {
        let at = match path.rfind('/') {
            Some(slash) => path[slash..].find(';').map(|i| slash + i),
            None => path.find(';'),
        };
        if let Some(at) = at {
            params = &path[at + 1..];
            path = &path[..at];
        }
    }
    Parts {
        scheme,
        netloc,
        path: path.to_owned(),
        params: params.to_owned(),
        query: query.to_owned(),
        fragment: fragment.to_owned(),
    }
}

/// `urlunparse`.
fn urlunparse(parts: &Parts) -> String {
    let mut url = parts.path.clone();
    if !parts.params.is_empty() {
        url = format!("{url};{}", parts.params);
    }
    if !parts.netloc.is_empty() {
        if !url.is_empty() && !url.starts_with('/') {
            url = format!("/{url}");
        }
        url = format!("//{}{url}", parts.netloc);
    } else if url.starts_with("//")
        || (!parts.scheme.is_empty()
            && USES_NETLOC.contains(&parts.scheme.as_str())
            && (url.is_empty() || url.starts_with('/')))
    {
        url = format!("//{url}");
    }
    if !parts.scheme.is_empty() {
        url = format!("{}:{url}", parts.scheme);
    }
    if !parts.query.is_empty() {
        url = format!("{url}?{}", parts.query);
    }
    if !parts.fragment.is_empty() {
        url = format!("{url}#{}", parts.fragment);
    }
    url
}

/// `urllib.parse.urljoin(base, url)`.
pub fn urljoin(base: &str, url: &str) -> String {
    if base.is_empty() {
        return url.to_owned();
    }
    if url.is_empty() {
        return base.to_owned();
    }
    let b = urlparse(base, "");
    let mut u = urlparse(url, &b.scheme);
    if u.scheme != b.scheme || !USES_RELATIVE.contains(&u.scheme.as_str()) {
        return url.to_owned();
    }
    if USES_NETLOC.contains(&u.scheme.as_str()) {
        if !u.netloc.is_empty() {
            return urlunparse(&u);
        }
        u.netloc = b.netloc.clone();
    }
    if u.path.is_empty() && u.params.is_empty() {
        u.path = b.path.clone();
        u.params = b.params.clone();
        if u.query.is_empty() {
            u.query = b.query.clone();
        }
        return urlunparse(&u);
    }
    let mut base_parts: Vec<&str> = b.path.split('/').collect();
    if base_parts.last() != Some(&"") {
        base_parts.pop();
    }
    let segments: Vec<&str> = if u.path.starts_with('/') {
        u.path.split('/').collect()
    } else {
        let mut all: Vec<&str> = base_parts;
        all.extend(u.path.split('/'));
        let n = all.len();
        if n > 2 {
            let inner: Vec<&str> = all[1..n - 1]
                .iter()
                .copied()
                .filter(|s| !s.is_empty())
                .collect();
            let (first, last) = (all[0], all[n - 1]);
            all = std::iter::once(first)
                .chain(inner)
                .chain(std::iter::once(last))
                .collect();
        }
        all
    };
    let mut resolved: Vec<&str> = Vec::new();
    for seg in &segments {
        match *seg {
            ".." => {
                resolved.pop();
            }
            "." => {}
            other => resolved.push(other),
        }
    }
    if matches!(segments.last(), Some(&".") | Some(&"..")) {
        resolved.push("");
    }
    let joined = resolved.join("/");
    u.path = if joined.is_empty() {
        "/".to_owned()
    } else {
        joined
    };
    urlunparse(&u)
}

#[cfg(test)]
mod tests;
