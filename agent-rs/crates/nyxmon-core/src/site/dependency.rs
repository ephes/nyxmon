//! Per-check site connectivity dependencies (`service_layer/site_dependency.py`).
//!
//! A check declares the connectivity paths it needs in
//! `health_check.data["site_dependency"]`. Malformed declarations warn once
//! per check and resolve to "unclassified" (`None`), which keeps the
//! notification behaviour of an undeclared check.

use std::fmt;

use crate::log::{WarnOnce, loggers};
use crate::pyjson::{PyJson, py_repr, py_strip};

/// `SITE_DEPENDENCY_DATA_KEY`.
pub const SITE_DEPENDENCY_DATA_KEY: &str = "site_dependency";

/// [`WarnOnce`] scope of `site_dependency.py:_warned_checks`.
pub const WARN_SCOPE: &str = "site_dependency";

/// [`WarnOnce`] scope of `site_connectivity.py:_warned_unobserved_checks`.
pub const UNOBSERVED_WARN_SCOPE: &str = "site_unobserved";

/// One of the connectivity dimensions the observer measures (`PATH_NAMES`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum PathName {
    Dns,
    Ipv4,
    Ipv6,
}

impl PathName {
    /// `PATH_NAMES`, in their Python order.
    pub const ALL: [PathName; 3] = [PathName::Dns, PathName::Ipv4, PathName::Ipv6];

    pub fn as_str(self) -> &'static str {
        match self {
            PathName::Dns => "dns",
            PathName::Ipv4 => "ipv4",
            PathName::Ipv6 => "ipv6",
        }
    }

    /// The exact name (no normalisation).
    pub fn parse(name: &str) -> Option<PathName> {
        PathName::ALL.into_iter().find(|path| path.as_str() == name)
    }

    /// Position in [`PathName::ALL`].
    pub fn index(self) -> usize {
        self as usize
    }
}

impl fmt::Display for PathName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// `SiteDependency`: one entry per requirement, each a list of alternative
/// paths (a single entry is a plain requirement, more form an any-of group).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SiteDependency {
    pub requirements: Vec<Vec<PathName>>,
}

impl SiteDependency {
    /// `INTERNET_REQUIREMENTS`: a name lookup plus one address family.
    pub fn internet() -> Self {
        SiteDependency {
            requirements: vec![vec![PathName::Dns], vec![PathName::Ipv4, PathName::Ipv6]],
        }
    }

    /// Every path named by any requirement, in declaration order.
    pub fn path_names(&self) -> Vec<PathName> {
        let mut seen = Vec::new();
        for name in self.requirements.iter().flatten() {
            if !seen.contains(name) {
                seen.push(*name);
            }
        }
        seen
    }
}

/// `str.strip().lower()`.
fn normalised(text: &str) -> String {
    py_strip(text).to_lowercase()
}

/// `_path_name`.
fn path_name(value: &PyJson) -> Option<PathName> {
    PathName::parse(&normalised(value.as_str()?))
}

/// `_requirement`: one normalised requirement, `None` when malformed.
fn requirement(value: &PyJson) -> Option<Vec<PathName>> {
    match value {
        PyJson::Str(_) => path_name(value).map(|name| vec![name]),
        PyJson::List(members) => {
            let mut names = Vec::new();
            for member in members {
                let name = path_name(member)?;
                if !names.contains(&name) {
                    names.push(name);
                }
            }
            (!names.is_empty()).then_some(names)
        }
        _ => None,
    }
}

/// `_warn_once`.
fn warn_once(warnings: &WarnOnce, check_id: i64, reason: &str, raw: &PyJson) {
    warnings.warn(loggers::SITE_DEPENDENCY, WARN_SCOPE, check_id, "", || {
        format!(
            "{SITE_DEPENDENCY_DATA_KEY} for check_id={check_id} is invalid ({reason}, got {}); \
             treating the check as unclassified",
            py_repr(raw)
        )
    });
}

/// `resolve_site_dependency(check)`: the parsed dependency of a check with
/// this `check_id` and raw `data`, or `None` when it is unclassified.
pub fn resolve_site_dependency(
    warnings: &WarnOnce,
    check_id: i64,
    data: &PyJson,
) -> Option<SiteDependency> {
    let raw = data.get(SITE_DEPENDENCY_DATA_KEY)?;
    match raw {
        PyJson::Null => None,
        PyJson::Str(text) => match normalised(text).as_str() {
            "none" | "" => None,
            "internet" => Some(SiteDependency::internet()),
            _ => {
                warn_once(warnings, check_id, "unknown shorthand", raw);
                None
            }
        },
        PyJson::Dict(declaration) => {
            let requires = match declaration.get("requires") {
                Some(PyJson::List(requires)) if !requires.is_empty() => requires,
                _ => {
                    warn_once(warnings, check_id, "requires must be a non-empty list", raw);
                    return None;
                }
            };
            let mut requirements: Vec<Vec<PathName>> = Vec::new();
            for entry in requires {
                let Some(parsed) = requirement(entry) else {
                    warn_once(
                        warnings,
                        check_id,
                        "every requirement must name one of dns, ipv4, ipv6",
                        raw,
                    );
                    return None;
                };
                if !requirements.contains(&parsed) {
                    requirements.push(parsed);
                }
            }
            Some(SiteDependency { requirements })
        }
        _ => {
            warn_once(warnings, check_id, "must be a string or an object", raw);
            None
        }
    }
}

/// `site_connectivity.py:warn_unobserved_requirements`: warn once per check
/// whose plain (single-path) requirement names a path nobody observes.
///
/// `checks` yields `(check_id, data)` pairs.
pub fn warn_unobserved_requirements<'a>(
    warnings: &WarnOnce,
    checks: impl IntoIterator<Item = (i64, &'a PyJson)>,
    unobserved: &[PathName],
) {
    if unobserved.is_empty() {
        return;
    }
    for (check_id, data) in checks {
        let Some(dependency) = resolve_site_dependency(warnings, check_id, data) else {
            continue;
        };
        let offenders: Vec<&str> = dependency
            .requirements
            .iter()
            .filter(|requirement| requirement.len() == 1 && unobserved.contains(&requirement[0]))
            .map(|requirement| requirement[0].as_str())
            .collect();
        if offenders.is_empty() {
            continue;
        }
        warnings.warn(
            loggers::SITE_CONNECTIVITY,
            UNOBSERVED_WARN_SCOPE,
            check_id,
            "",
            || {
                format!(
                    "check_id={check_id} requires the unobserved path(s) {}; the requirement is \
                     always met and can never hold a notification",
                    offenders.join(", ")
                )
            },
        );
    }
}
