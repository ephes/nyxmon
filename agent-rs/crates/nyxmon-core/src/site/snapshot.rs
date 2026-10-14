//! The immutable connectivity snapshot and its hold logic
//! (`site_connectivity.py:PathSnapshot`, `SiteConnectivitySnapshot`).

use std::fmt;

use super::dependency::{PathName, SiteDependency};
use crate::env::SiteMode;
use crate::pyjson::{PyDict, PyJson};

/// `REASON_DEPENDENCY_DOWN`.
pub const REASON_DEPENDENCY_DOWN: &str = "dependency_down";
/// `REASON_DEPENDENCY_RECOVERING`.
pub const REASON_DEPENDENCY_RECOVERING: &str = "dependency_recovering";
/// `REASON_MEASURED_BEFORE_RELEASE`.
pub const REASON_MEASURED_BEFORE_RELEASE: &str = "measured_before_release";

/// `PathState`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum PathState {
    Up,
    Failing,
    Down,
    Recovering,
    Unobserved,
}

impl PathState {
    pub fn as_str(self) -> &'static str {
        match self {
            PathState::Up => "up",
            PathState::Failing => "failing",
            PathState::Down => "down",
            PathState::Recovering => "recovering",
            PathState::Unobserved => "unobserved",
        }
    }

    pub fn parse(value: &str) -> Option<PathState> {
        [
            PathState::Up,
            PathState::Failing,
            PathState::Down,
            PathState::Recovering,
            PathState::Unobserved,
        ]
        .into_iter()
        .find(|state| state.as_str() == value)
    }

    /// `USABLE_STATES`: `failing` is usable on purpose, an unconfirmed
    /// failure never defers an alert.
    pub fn is_usable(self) -> bool {
        matches!(self, PathState::Up | PathState::Failing)
    }

    /// `BLOCKING_STATES`.
    pub fn is_blocking(self) -> bool {
        matches!(self, PathState::Down | PathState::Recovering)
    }
}

impl fmt::Display for PathState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// `PathSnapshot`: one path at the end of a probe round.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PathSnapshot {
    pub state: PathState,
    pub down_since: i64,
    pub recovered_at: i64,
    pub release_at: i64,
    pub last_release_at: i64,
}

impl PathSnapshot {
    /// `PathSnapshot(state)` with every timestamp 0.
    pub fn new(state: PathState) -> Self {
        PathSnapshot {
            state,
            down_since: 0,
            recovered_at: 0,
            release_at: 0,
            last_release_at: 0,
        }
    }
}

/// Why a sample's notification is (or would be) held: the reason dict of
/// `SiteConnectivitySnapshot._reason`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HoldReason {
    /// One of the `REASON_*` constants.
    pub reason: &'static str,
    pub paths: Vec<PathName>,
    pub down_since: i64,
    pub state: PathState,
    pub incident_id: i64,
    /// Only for [`REASON_MEASURED_BEFORE_RELEASE`].
    pub release_at: Option<i64>,
}

impl HoldReason {
    /// The reason dict, keys in Python order.
    pub fn to_dict(&self) -> PyDict {
        let mut dict = PyDict::new();
        dict.insert("reason".to_owned(), PyJson::from(self.reason));
        dict.insert(
            "paths".to_owned(),
            PyJson::List(
                self.paths
                    .iter()
                    .map(|p| PyJson::from(p.as_str()))
                    .collect(),
            ),
        );
        dict.insert("down_since".to_owned(), PyJson::Int(self.down_since));
        dict.insert("state".to_owned(), PyJson::from(self.state.as_str()));
        dict.insert("incident_id".to_owned(), PyJson::Int(self.incident_id));
        if let Some(release_at) = self.release_at {
            dict.insert("release_at".to_owned(), PyJson::Int(release_at));
        }
        dict
    }
}

/// `SiteConnectivitySnapshot`: the view the result handler reads without
/// touching the store.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SiteSnapshot {
    /// Indexed by [`PathName::index`]; `None` for a path the snapshot does
    /// not know (`paths.get(name) is None`).
    pub paths: [Option<PathSnapshot>; 3],
    pub observed_at: i64,
    pub mode: SiteMode,
    pub incident_id: i64,
}

impl SiteSnapshot {
    pub fn path(&self, name: PathName) -> Option<&PathSnapshot> {
        self.paths[name.index()].as_ref()
    }

    /// `is_fresh`.
    pub fn is_fresh(&self, now: i64, stale_after: i64) -> bool {
        i128::from(now) - i128::from(self.observed_at) <= i128::from(stale_after)
    }

    /// `_observed`: the members that are known and not `unobserved`.
    fn observed<'a>(
        &'a self,
        requirement: &'a [PathName],
    ) -> impl Iterator<Item = &'a PathSnapshot> {
        requirement
            .iter()
            .filter_map(|name| self.path(*name))
            .filter(|path| path.state != PathState::Unobserved)
    }

    /// `_requirement_met`: met unless every observed member blocks; a group
    /// without observed members is met.
    fn requirement_met(&self, requirement: &[PathName]) -> bool {
        let mut observed = self.observed(requirement).peekable();
        if observed.peek().is_none() {
            return true;
        }
        observed.any(|path| path.state.is_usable())
    }

    /// `_requirement_usable_since`: the earliest release among the members
    /// usable now, 0 when none is usable.
    fn requirement_usable_since(&self, requirement: &[PathName]) -> i64 {
        self.observed(requirement)
            .filter(|path| path.state.is_usable())
            .map(|path| path.last_release_at)
            .min()
            .unwrap_or(0)
    }

    /// `dependency_usable`: every requirement met right now; `false` for an
    /// unclassified check.
    pub fn dependency_usable(&self, dependency: Option<&SiteDependency>) -> bool {
        dependency.is_some_and(|dependency| {
            dependency
                .requirements
                .iter()
                .all(|requirement| self.requirement_met(requirement))
        })
    }

    /// `_reason`.
    fn reason(&self, dependency: &SiteDependency, claim_started_at: i64) -> Option<HoldReason> {
        let mut blocked: Vec<PathName> = Vec::new();
        for requirement in &dependency.requirements {
            if self.requirement_met(requirement) {
                continue;
            }
            for name in requirement {
                let Some(path) = self.path(*name) else {
                    continue;
                };
                if path.state.is_blocking() && !blocked.contains(name) {
                    blocked.push(*name);
                }
            }
        }
        if !blocked.is_empty() {
            let blocked_paths = || blocked.iter().filter_map(|name| self.path(*name));
            let down = blocked_paths().any(|path| path.state == PathState::Down);
            let down_since = blocked_paths()
                .map(|path| path.down_since)
                .filter(|since| *since != 0)
                .min()
                .unwrap_or(0);
            return Some(HoldReason {
                reason: if down {
                    REASON_DEPENDENCY_DOWN
                } else {
                    REASON_DEPENDENCY_RECOVERING
                },
                down_since,
                state: if down {
                    PathState::Down
                } else {
                    PathState::Recovering
                },
                incident_id: self.incident_id,
                release_at: None,
                paths: blocked,
            });
        }
        if claim_started_at <= 0 {
            // No claim time: freshness cannot be judged, and a permanent
            // watermark must never hold such a sample forever.
            return None;
        }
        let mut late: Vec<PathName> = Vec::new();
        let mut watermark = 0;
        for requirement in &dependency.requirements {
            let usable_since = self.requirement_usable_since(requirement);
            if usable_since <= claim_started_at {
                continue;
            }
            watermark = watermark.max(usable_since);
            for name in requirement {
                let Some(path) = self.path(*name) else {
                    continue;
                };
                if !path.state.is_usable()
                    || path.last_release_at <= claim_started_at
                    || late.contains(name)
                {
                    continue;
                }
                late.push(*name);
            }
        }
        if late.is_empty() {
            return None;
        }
        Some(HoldReason {
            reason: REASON_MEASURED_BEFORE_RELEASE,
            paths: late,
            down_since: 0,
            state: PathState::Up,
            incident_id: self.incident_id,
            release_at: Some(watermark),
        })
    }

    /// `hold_reason`: why the sample must be held; only in `enforce`.
    pub fn hold_reason(
        &self,
        dependency: Option<&SiteDependency>,
        claim_started_at: i64,
        now: i64,
        stale_after: i64,
    ) -> Option<HoldReason> {
        if self.mode != SiteMode::Enforce {
            return None;
        }
        self.observed_reason(dependency, claim_started_at, now, stale_after)
    }

    /// `observed_reason`: like [`SiteSnapshot::hold_reason`], ignoring the
    /// mode; `None` for an unclassified check or a stale snapshot.
    pub fn observed_reason(
        &self,
        dependency: Option<&SiteDependency>,
        claim_started_at: i64,
        now: i64,
        stale_after: i64,
    ) -> Option<HoldReason> {
        let dependency = dependency?;
        if !self.is_fresh(now, stale_after) {
            return None;
        }
        self.reason(dependency, claim_started_at)
    }

    /// `dependency_recovered`: a fresh snapshot gives no reason to hold.
    pub fn dependency_recovered(
        &self,
        dependency: Option<&SiteDependency>,
        claim_started_at: i64,
        now: i64,
        stale_after: i64,
    ) -> bool {
        let Some(dependency) = dependency else {
            return false;
        };
        self.is_fresh(now, stale_after) && self.reason(dependency, claim_started_at).is_none()
    }
}
