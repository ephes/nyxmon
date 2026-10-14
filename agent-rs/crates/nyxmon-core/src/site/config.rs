//! Configuration helpers of the observer (`SiteConnectivityConfig`, PLAN.md
//! section 6.1). Parsing from the environment lives in
//! [`crate::env::site_env_config`]; this adds the per-path view.

use super::dependency::PathName;
use crate::env::{ProbeTarget, SiteEnvConfig};

/// `SiteConnectivityConfig`.
pub type SiteConfig = SiteEnvConfig;

/// `PROBE_ROUND_SLACK_SECONDS`: hard budget of one probe round on top of the
/// per-target timeout (used by the I/O driver).
pub const PROBE_ROUND_SLACK_SECONDS: i64 = 2;

impl SiteEnvConfig {
    /// `targets_for(path)`: the configured targets; empty means unobserved.
    pub fn targets_for(&self, path: PathName) -> &[ProbeTarget] {
        match path {
            PathName::Dns => &self.dns_names,
            PathName::Ipv4 => &self.ipv4_targets,
            PathName::Ipv6 => &self.ipv6_targets,
        }
    }

    /// Every target of one probe round, in `PATH_NAMES` order (`_targets`).
    pub fn all_targets(&self) -> Vec<ProbeTarget> {
        PathName::ALL
            .into_iter()
            .flat_map(|path| self.targets_for(path).iter().cloned())
            .collect()
    }

    /// The probe round budget in seconds (`probe_timeout + 2`).
    pub fn probe_round_budget_seconds(&self) -> i64 {
        self.probe_timeout.saturating_add(PROBE_ROUND_SLACK_SECONDS)
    }
}
