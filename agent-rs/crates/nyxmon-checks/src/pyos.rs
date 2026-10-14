//! `str(exc)` of Python's `OSError` family for Rust I/O errors.
//!
//! Operators see these texts on the check detail page, so OS-level failures
//! are rendered in the Python form `[Errno N] <strerror>` wherever the error
//! number is known (PLAN.md section 1.3, P2).

use std::io;

/// The prefix the standard library puts before a `gai_strerror` text.
const GAI_PREFIX: &str = "failed to lookup address information: ";

/// `gai_strerror` texts and their `EAI_*` codes (glibc).
#[cfg(not(target_vendor = "apple"))]
const GAI_CODES: &[(i32, &str)] = &[
    (-1, "Bad value for ai_flags"),
    (-2, "Name or service not known"),
    (-3, "Temporary failure in name resolution"),
    (-4, "Non-recoverable failure in name resolution"),
    (-5, "No address associated with hostname"),
    (-6, "ai_family not supported"),
    (-7, "ai_socktype not supported"),
    (-8, "Servname not supported for ai_socktype"),
    (-9, "Address family for hostname not supported"),
    (-10, "Memory allocation failure"),
    (-11, "System error"),
    (-12, "Argument buffer overflow"),
    (-105, "Parameter string not correctly encoded"),
];

/// `gai_strerror` texts and their `EAI_*` codes (macOS).
#[cfg(target_vendor = "apple")]
const GAI_CODES: &[(i32, &str)] = &[
    (1, "Address family for nodename not supported"),
    (2, "Temporary failure in name resolution"),
    (3, "Invalid value for ai_flags"),
    (4, "Non-recoverable failure in name resolution"),
    (5, "ai_family not supported"),
    (6, "Memory allocation failure"),
    (7, "No address associated with nodename"),
    (8, "nodename nor servname provided, or not known"),
    (9, "servname not supported for ai_socktype"),
    (10, "ai_socktype not supported"),
    (11, "System error"),
    (12, "Invalid value for hints"),
    (13, "Resolved protocol is unknown"),
    (14, "Argument buffer overflow"),
];

/// The `strerror` text of an OS error without Rust's ` (os error N)` suffix.
fn strerror(err: &io::Error, code: i32) -> String {
    let text = err.to_string();
    let suffix = format!(" (os error {code})");
    text.strip_suffix(&suffix).unwrap_or(&text).to_owned()
}

/// `str(exc)` for the `OSError` Python raises for this error: `[Errno N]
/// <strerror>` for OS errors and `getaddrinfo` failures (`socket.gaierror`),
/// otherwise the Rust text.
pub fn os_error_str(err: &io::Error) -> String {
    if let Some(code) = err.raw_os_error() {
        return format!("[Errno {code}] {}", strerror(err, code));
    }
    let text = err.to_string();
    if let Some(detail) = text.strip_prefix(GAI_PREFIX) {
        if let Some((code, _)) = GAI_CODES.iter().find(|(_, message)| *message == detail) {
            return format!("[Errno {code}] {detail}");
        }
        return detail.to_owned();
    }
    text
}

/// `str(exc)` for an `OSError` raised with a file name, such as the
/// `FileNotFoundError` of starting a missing program:
/// `[Errno 2] No such file or directory: '/bin/ping'`.
pub fn os_error_with_filename(err: &io::Error, filename: &str) -> String {
    match err.raw_os_error() {
        Some(code) => format!(
            "[Errno {code}] {}: {}",
            strerror(err, code),
            nyxmon_core::pyjson::py_str_repr(filename)
        ),
        None => os_error_str(err),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn os_errors_use_the_python_form() {
        let refused = io::Error::from_raw_os_error(111);
        let rendered = os_error_str(&refused);
        assert!(rendered.starts_with("[Errno 111] "), "{rendered}");
        assert!(!rendered.contains("os error"), "{rendered}");
        let missing = io::Error::from_raw_os_error(2);
        assert_eq!(
            os_error_with_filename(&missing, "/bin/ping"),
            "[Errno 2] No such file or directory: '/bin/ping'"
        );
    }

    #[test]
    fn resolver_errors_get_their_gai_code() {
        for (code, message) in GAI_CODES {
            let err = io::Error::other(format!("{GAI_PREFIX}{message}"));
            assert_eq!(os_error_str(&err), format!("[Errno {code}] {message}"));
        }
        let unknown = io::Error::other(format!("{GAI_PREFIX}something new"));
        assert_eq!(os_error_str(&unknown), "something new");
        assert_eq!(os_error_str(&io::Error::other("plain")), "plain");
    }
}
