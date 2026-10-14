//! Site connectivity: per-check dependencies (`service_layer/site_dependency.py`),
//! the immutable snapshot the result handler consults
//! (`adapters/site_connectivity.py:SiteConnectivitySnapshot`), and the pure
//! observer state machine with its payload codec and message rendering
//! (`SiteConnectivityObserver`), PLAN.md section 6.

pub mod config;
pub mod dependency;
pub mod observer;
pub mod render;
#[cfg(test)]
mod replay_tests;
pub mod snapshot;

pub use crate::env::SiteMode;
pub use config::SiteConfig;
pub use dependency::{
    PathName, SiteDependency, resolve_site_dependency, warn_unobserved_requirements,
};
pub use observer::{
    LogLevel, Observer, Phase, PortError, ProbeOutcome, SiteDelivery, SiteMessage, SitePorts,
};
pub use render::Raised;
pub use snapshot::{HoldReason, PathSnapshot, PathState, SiteSnapshot};
