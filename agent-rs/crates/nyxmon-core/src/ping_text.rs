//! The pure parts of the ping executor (`executors/ping_executor.py`): the
//! command line, the output parsers and the statistics. PLAN.md section
//! 5.13; pinned by `fixtures/ping_text.json`.
//!
//! The four regular expressions of the Python module are matched by hand
//! with the semantics of CPython's `re` on `str` patterns: `\s` is
//! `str.isspace`, `\w`/`\b` use the oracle's `str.isalnum` table
//! (`fixtures/ping_unicode.json`), `IGNORECASE` letters match their
//! CPython equivalence class (`i` also matches U+0130 and U+0131, `s` also
//! U+017F), and the backtracking order of the greedy groups is kept.

use std::net::IpAddr;
use std::sync::OnceLock;

use crate::config::PyException;
use crate::pyjson::{PyDict, PyJson, loads, py_isspace, py_lower, py_round, py_strip};
use crate::pyurl::py_ip_address;

/// `PROCESS_GRACE_SECONDS`: added to the per-attempt timeout before the
/// process is killed.
pub const PROCESS_GRACE_SECONDS: f64 = 2.0;

/// `FALLBACK_PING_PATHS`.
pub const FALLBACK_PING_PATHS: [&str; 4] =
    ["/sbin/ping", "/bin/ping", "/usr/bin/ping", "/usr/sbin/ping"];

/// `FALLBACK_PING6_PATHS`.
pub const FALLBACK_PING6_PATHS: [&str; 3] = ["/sbin/ping6", "/usr/sbin/ping6", "/bin/ping6"];

/// `MAX_ERROR_DETAIL`: code points kept of a failure line.
pub const MAX_ERROR_DETAIL: usize = 200;

/// `PERMISSION_HINT`.
pub const PERMISSION_HINT: &str = "The system ping binary lacks the privilege to send ICMP \
echo requests. On Linux grant it CAP_NET_RAW (setcap cap_net_raw+ep $(command -v ping)) or \
allow unprivileged ICMP via sysctl net.ipv4.ping_group_range; on macOS/BSD ping must be \
setuid root (the default).";

const PERMISSION_MARKERS: [&str; 6] = [
    "operation not permitted",
    "permission denied",
    "must be root",
    "must run as root",
    "requires root",
    "access denied",
];

const DETAIL_WORDS: [&str; 9] = [
    "unreachable",
    "unknown",
    "error",
    "denied",
    "permitted",
    "no route",
    "cannot",
    "failed",
    "exceeded",
];

/// `sys.platform` of the running system.
pub fn sys_platform() -> &'static str {
    match std::env::consts::OS {
        "macos" => "darwin",
        "windows" => "win32",
        other => other,
    }
}

/// `_parse_ip`: `ipaddress.ip_address(value.split("%", 1)[0])`.
pub fn parse_ip(value: &str) -> Option<IpAddr> {
    let address = value.split('%').next().unwrap_or("");
    py_ip_address(address).map(|ip| ip.addr)
}

/// `str(n)` of `max(1, math.ceil(seconds))`.
fn ceil_at_least_one(seconds: f64) -> String {
    let ceiled = seconds.ceil();
    // `timeout` is at most 60 s, so the value fits.
    format!("{}", if ceiled < 1.0 { 1 } else { ceiled as i64 })
}

/// `build_ping_command`: a single-echo command line for `platform` (a
/// `sys.platform` value).
///
/// # Errors
/// `ValueError` when `address` is not an IP address (Python's
/// `ipaddress.ip_address` raises and the exception escapes the executor).
pub fn build_ping_command(
    platform: &str,
    address: &str,
    timeout: f64,
    ping_binary: &str,
    ping6_binary: Option<&str>,
) -> Result<Vec<String>, PyException> {
    let is_ipv6 = parse_ip(address).ok_or(PyException::VALUE_ERROR)?.is_ipv6();
    let wait_ms = ceil_at_least_one(timeout * 1000.0);
    let owned = |parts: &[&str]| parts.iter().map(|part| (*part).to_owned()).collect();

    if platform.starts_with("win") {
        let mut command: Vec<String> = owned(&[ping_binary, "-n", "1", "-w", &wait_ms]);
        if is_ipv6 {
            command.push("-6".to_owned());
        }
        command.push(address.to_owned());
        return Ok(command);
    }
    if platform.starts_with("linux") {
        let wait_s = ceil_at_least_one(timeout);
        return Ok(owned(&[
            ping_binary,
            "-n",
            "-c",
            "1",
            "-W",
            &wait_s,
            address,
        ]));
    }
    if is_ipv6 {
        // `ping6_binary or "ping6"`.
        let ping6 = ping6_binary
            .filter(|path| !path.is_empty())
            .unwrap_or("ping6");
        return Ok(owned(&[ping6, "-n", "-c", "1", address]));
    }
    if platform.starts_with("darwin") || platform.starts_with("freebsd") {
        return Ok(owned(&[
            ping_binary,
            "-n",
            "-c",
            "1",
            "-W",
            &wait_ms,
            address,
        ]));
    }
    Ok(owned(&[ping_binary, "-n", "-c", "1", address]))
}

// ------------------------------------------------------------ re emulation

struct WordTable {
    ranges: Vec<(u32, u32)>,
}

/// The oracle's `\w` (`str.isalnum()` or `_`) as code point ranges.
fn word_table() -> &'static WordTable {
    static TABLE: OnceLock<WordTable> = OnceLock::new();
    TABLE.get_or_init(|| {
        let table = loads(include_str!("../../../fixtures/ping_unicode.json"))
            .expect("the generated Unicode table is valid JSON");
        let bound = |range: &PyJson, index: usize| {
            range
                .as_list()
                .and_then(|r| r.get(index))
                .and_then(PyJson::as_i64)
                .and_then(|v| u32::try_from(v).ok())
                .expect("a code point range")
        };
        let ranges = table
            .get("word")
            .and_then(PyJson::as_list)
            .expect("the word ranges")
            .iter()
            .map(|range| (bound(range, 0), bound(range, 1)))
            .collect();
        WordTable { ranges }
    })
}

/// `re`'s `\w` for a `str` pattern.
pub fn is_word(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_alphanumeric() || c == '_';
    }
    let code = c as u32;
    let ranges = &word_table().ranges;
    let index = ranges.partition_point(|&(start, _)| start <= code);
    index > 0 && ranges[index - 1].1 >= code
}

/// Whether `c` matches the pattern letter `letter` under `re.IGNORECASE`
/// (the letters of `time`, `ttl` and `ms`).
pub fn matches_ignorecase(c: char, letter: char) -> bool {
    match letter {
        'i' => matches!(c, 'i' | 'I' | '\u{130}' | '\u{131}'),
        's' => matches!(c, 's' | 'S' | '\u{17f}'),
        _ => c.eq_ignore_ascii_case(&letter),
    }
}

fn skip_space(chars: &[char], mut i: usize) -> usize {
    while i < chars.len() && py_isspace(chars[i]) {
        i += 1;
    }
    i
}

fn skip_digits(chars: &[char], mut i: usize) -> usize {
    while i < chars.len() && chars[i].is_ascii_digit() {
        i += 1;
    }
    i
}

fn literal_ignorecase(chars: &[char], i: usize, word: &str) -> bool {
    let letters = word.chars().count();
    i + letters <= chars.len()
        && chars[i..i + letters]
            .iter()
            .zip(word.chars())
            .all(|(&c, letter)| matches_ignorecase(c, letter))
}

/// The ends of `[0-9]+(?:[.,][0-9]+)?` at `i`, in the order the greedy
/// match tries them (with the fraction first).
fn number_ends(chars: &[char], i: usize) -> Vec<usize> {
    let integer_end = skip_digits(chars, i);
    if integer_end == i {
        return Vec::new();
    }
    let mut ends = Vec::with_capacity(2);
    if integer_end < chars.len() && matches!(chars[integer_end], '.' | ',') {
        let fraction_end = skip_digits(chars, integer_end + 1);
        if fraction_end > integer_end + 1 {
            ends.push(fraction_end);
        }
    }
    ends.push(integer_end);
    ends
}

/// `float(group.replace(",", "."))` of a matched number.
fn number_value(chars: &[char]) -> f64 {
    let text: String = chars
        .iter()
        .map(|&c| if c == ',' { '.' } else { c })
        .collect();
    // ASCII digits with at most one `.`: always a valid float literal.
    text.parse().unwrap_or(f64::NAN)
}

/// After `[=<]` at `i`: `\s*([0-9]+(?:[.,][0-9]+)?)\s*ms`, returning the
/// value and the index after `ms`.
fn value_then_ms(chars: &[char], i: usize, word_boundary: bool) -> Option<f64> {
    if i >= chars.len() || !matches!(chars[i], '=' | '<') {
        return None;
    }
    let start = skip_space(chars, i + 1);
    for end in number_ends(chars, start) {
        let unit = skip_space(chars, end);
        if !literal_ignorecase(chars, unit, "ms") {
            continue;
        }
        if word_boundary {
            // `\b` after the `s`, a word character.
            let after = chars.get(unit + 2).copied();
            if after.is_some_and(is_word) == is_word(chars[unit + 1]) {
                continue;
            }
        }
        return Some(number_value(&chars[start..end]));
    }
    None
}

/// `_RTT_RE.search(text)`:
/// `time\s*[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*ms`, ignoring case.
fn search_rtt(chars: &[char]) -> Option<f64> {
    (0..chars.len()).find_map(|p| {
        if !literal_ignorecase(chars, p, "time") {
            return None;
        }
        value_then_ms(chars, skip_space(chars, p + 4), false)
    })
}

/// `_LOCALIZED_RTT_RE.search(line)`:
/// `[=<]\s*([0-9]+(?:[.,][0-9]+)?)\s*ms\b`, ignoring case.
fn search_localized_rtt(chars: &[char]) -> Option<f64> {
    (0..chars.len()).find_map(|p| value_then_ms(chars, p, true))
}

/// `_TTL_RE.search(line)`: `\bttl\s*=\s*[0-9]+`, ignoring case.
fn has_ttl(chars: &[char]) -> bool {
    (0..chars.len()).any(|p| {
        // `\b` before the `t`, a word character.
        if p > 0 && is_word(chars[p - 1]) {
            return false;
        }
        if !literal_ignorecase(chars, p, "ttl") {
            return false;
        }
        let equals = skip_space(chars, p + 3);
        if equals >= chars.len() || chars[equals] != '=' {
            return false;
        }
        let digits = skip_space(chars, equals + 1);
        digits < chars.len() && chars[digits].is_ascii_digit()
    })
}

fn is_source_char(c: char) -> bool {
    c.is_ascii_hexdigit() || c == ':' || c == '.'
}

/// One `_SOURCE_RE` match at `p`: the group and the index after the match.
///
/// `(?<![0-9A-Za-z:.%])([0-9A-Fa-f:.]+(?:%[0-9A-Za-z]+)?)\s*:(?:\s|$)`
fn source_at(chars: &[char], p: usize) -> Option<(usize, usize)> {
    if p > 0 {
        let before = chars[p - 1];
        if before.is_ascii_alphanumeric() || matches!(before, ':' | '.' | '%') {
            return None;
        }
    }
    let run = chars[p..]
        .iter()
        .take_while(|&&c| is_source_char(c))
        .count();
    // The greedy address part gives back one character at a time.
    for length in (1..=run).rev() {
        let mut group_end = p + length;
        if group_end < chars.len() && chars[group_end] == '%' {
            // The zone is all or nothing here: a shorter zone is followed by
            // an alphanumeric, a missing one by `%`, and neither is `\s` or
            // `:`.
            let zone = chars[group_end + 1..]
                .iter()
                .take_while(|c| c.is_ascii_alphanumeric())
                .count();
            if zone == 0 {
                continue;
            }
            group_end += 1 + zone;
        }
        let colon = skip_space(chars, group_end);
        if colon >= chars.len() || chars[colon] != ':' {
            continue;
        }
        return match chars.get(colon + 1) {
            None => Some((group_end, colon + 1)),
            Some(&c) if py_isspace(c) => Some((group_end, colon + 2)),
            Some(_) => continue,
        };
    }
    None
}

/// `_names_source`: whether a `_SOURCE_RE.finditer(line)` group parses as
/// `target`.
fn names_source(chars: &[char], target: IpAddr) -> bool {
    let mut p = 0;
    while p < chars.len() {
        match source_at(chars, p) {
            Some((group_end, match_end)) => {
                let group: String = chars[p..group_end].iter().collect();
                if parse_ip(&group) == Some(target) {
                    return true;
                }
                p = match_end;
            }
            None => p += 1,
        }
    }
    false
}

/// `str.splitlines()` (no line ends kept).
pub fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, c)) = chars.next() {
        if !matches!(
            c,
            '\n' | '\r'
                | '\u{b}'
                | '\u{c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            continue;
        }
        lines.push(&text[start..index]);
        start = index + c.len_utf8();
        if c == '\r' && chars.peek().is_some_and(|&(_, next)| next == '\n') {
            chars.next();
            start += 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// `parse_rtt_ms(output, address)`: the round-trip time of the first reply.
pub fn parse_rtt_ms(output: &str, address: Option<&str>) -> Option<f64> {
    let chars: Vec<char> = output.chars().collect();
    if let Some(rtt) = search_rtt(&chars) {
        return Some(rtt);
    }
    let target = address.filter(|a| !a.is_empty()).and_then(parse_ip);
    for line in py_splitlines(output) {
        let chars: Vec<char> = line.chars().collect();
        let reply = has_ttl(&chars) || target.is_some_and(|target| names_source(&chars, target));
        if reply && let Some(rtt) = search_localized_rtt(&chars) {
            return Some(rtt);
        }
    }
    None
}

/// `is_permission_failure(output)`.
pub fn is_permission_failure(output: &str) -> bool {
    let lowered = py_lower(output);
    PERMISSION_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
}

/// `text[:n]` in code points.
fn prefix(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// `_error_detail(output)`: the first line naming a failure (stderr before
/// stdout), else the last line of stderr, else `""`.
pub fn error_detail(stdout: &str, stderr: &str) -> String {
    for (index, text) in [stderr, stdout].into_iter().enumerate() {
        let lines: Vec<&str> = py_splitlines(text)
            .into_iter()
            .map(py_strip)
            .filter(|line| !line.is_empty())
            .collect();
        let interesting = lines.iter().find(|line| {
            let lowered = py_lower(line);
            DETAIL_WORDS.iter().any(|word| lowered.contains(word))
        });
        if let Some(line) = interesting {
            return prefix(line, MAX_ERROR_DETAIL);
        }
        if index == 0
            && let Some(last) = lines.last()
        {
            return prefix(last, MAX_ERROR_DETAIL);
        }
    }
    String::new()
}

/// `PingCheckExecutor._packet_stats(sent, received)`.
pub fn packet_stats(sent: i64, received: i64) -> PyDict {
    let loss = if sent == 0 {
        0.0
    } else {
        py_round((sent - received) as f64 * 100.0 / sent as f64, 1)
    };
    let mut stats = PyDict::new();
    stats.insert("packets_sent".to_owned(), PyJson::Int(sent));
    stats.insert("packets_received".to_owned(), PyJson::Int(received));
    stats.insert("packet_loss_percent".to_owned(), PyJson::Float(loss));
    stats
}

/// Python's `sum()` of floats (CPython 3.12+: Neumaier's compensated sum,
/// started from the integer `0`).
pub fn py_sum(values: &[f64]) -> f64 {
    let Some((&first, rest)) = values.split_first() else {
        return 0.0;
    };
    let mut total = 0.0 + first;
    let mut compensation = 0.0;
    for &x in rest {
        let t = total + x;
        if total.abs() >= x.abs() {
            compensation += (total - t) + x;
        } else {
            compensation += (x - t) + total;
        }
        total = t;
    }
    if compensation != 0.0 && compensation.is_finite() {
        total += compensation;
    }
    total
}

/// `min`, `max` and `round(sum(rtts) / len(rtts), 3)` of a non-empty list.
pub fn rtt_stats(rtts: &[f64]) -> (f64, f64, f64) {
    let mut min = rtts[0];
    let mut max = rtts[0];
    for &rtt in &rtts[1..] {
        if rtt < min {
            min = rtt;
        }
        if rtt > max {
            max = rtt;
        }
    }
    let avg = py_round(py_sum(rtts) / rtts.len() as f64, 3);
    (min, max, avg)
}

/// `bytes.decode(errors="replace")` of process output.
pub fn decode_output(data: &[u8]) -> String {
    String::from_utf8_lossy(data).into_owned()
}

#[cfg(test)]
mod tests;
