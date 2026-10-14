//! Startup validation (`startup_validation.py:validate_check_types`).

use std::collections::BTreeSet;

use nyxmon_core::model::Check;
use nyxmon_core::pyjson::py_str_repr;

/// A Python set of strings rendered sorted (`{'a', 'b'}`; PLAN.md 1.3 P2:
/// Python's order depends on `PYTHONHASHSEED`).
pub fn sorted_set_repr<'a>(items: impl IntoIterator<Item = &'a str>) -> String {
    let items: BTreeSet<&str> = items.into_iter().collect();
    if items.is_empty() {
        return "set()".to_owned();
    }
    let parts: Vec<String> = items.into_iter().map(py_str_repr).collect();
    format!("{{{}}}", parts.join(", "))
}

/// Log whether every persisted check has a registered executor (logs only).
pub fn validate_check_types(checks: &[Check], registered: &[&str]) {
    if checks.is_empty() {
        tracing::info!(
            target: "nyxmon.startup_validation",
            "No checks found in database - skipping check type validation"
        );
        return;
    }
    let registered_repr = sorted_set_repr(registered.iter().copied());
    let unknown: Vec<&Check> = checks
        .iter()
        .filter(|check| !registered.contains(&check.check_type.as_str()))
        .collect();
    if unknown.is_empty() {
        tracing::info!(
            target: "nyxmon.startup_validation",
            "Validated {} check(s) - all types registered: {registered_repr}",
            checks.len()
        );
        return;
    }
    let unknown_types = sorted_set_repr(unknown.iter().map(|check| check.check_type.as_str()));
    tracing::warn!(
        target: "nyxmon.startup_validation",
        "Found {} check(s) with unregistered types: {unknown_types}",
        unknown.len()
    );
    tracing::warn!(
        target: "nyxmon.startup_validation",
        "These checks will fail when executed. Registered types: {registered_repr}"
    );
    for check in &unknown {
        tracing::warn!(
            target: "nyxmon.startup_validation",
            "  - Check #{} '{}': type='{}' (unregistered)",
            check.check_id,
            check.name,
            check.check_type
        );
    }
    tracing::warn!(
        target: "nyxmon.startup_validation",
        "Action required: Update or remove checks with unregistered types, or register \
         executors for these types"
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::logging;
    use crate::testutil::check;

    const REGISTERED: [&str; 3] = ["http", "dns", "tcp"];

    #[test]
    fn no_checks_logs_info() {
        let (capture, _guard) = logging::capture();
        validate_check_types(&[], &REGISTERED);
        assert_eq!(
            capture.messages(),
            ["No checks found in database - skipping check type validation"]
        );
        assert_eq!(capture.lines()[0].level, "INFO");
    }

    #[test]
    fn all_registered_types_log_success() {
        let (capture, _guard) = logging::capture();
        let mut dns = check(2);
        dns.check_type = "dns".to_owned();
        validate_check_types(&[check(1), dns], &REGISTERED);
        assert_eq!(
            capture.messages(),
            ["Validated 2 check(s) - all types registered: {'dns', 'http', 'tcp'}"]
        );
        assert_eq!(capture.lines()[0].logger, "nyxmon.startup_validation");
    }

    #[test]
    fn unregistered_types_are_listed_per_check() {
        let (capture, _guard) = logging::capture();
        let mut legacy = check(7);
        legacy.check_type = "legacy".to_owned();
        legacy.name = "Old".to_owned();
        let mut other = check(9);
        other.check_type = "custom".to_owned();
        other.name = "It's".to_owned();
        validate_check_types(&[check(1), legacy, other], &REGISTERED);
        assert_eq!(
            capture.messages(),
            [
                "Found 2 check(s) with unregistered types: {'custom', 'legacy'}",
                "These checks will fail when executed. Registered types: {'dns', 'http', 'tcp'}",
                "  - Check #7 'Old': type='legacy' (unregistered)",
                "  - Check #9 'It's': type='custom' (unregistered)",
                "Action required: Update or remove checks with unregistered types, or register \
                 executors for these types",
            ]
        );
        assert!(capture.lines().iter().all(|line| line.level == "WARNING"));
    }

    #[test]
    fn set_reprs_follow_python() {
        assert_eq!(sorted_set_repr(["b", "a", "b"]), "{'a', 'b'}");
        assert_eq!(sorted_set_repr(["it's"]), "{\"it's\"}");
        assert_eq!(sorted_set_repr([]), "set()");
    }
}
