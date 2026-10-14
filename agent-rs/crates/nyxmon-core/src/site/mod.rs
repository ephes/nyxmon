//! Site connectivity: per-check dependencies (`service_layer/site_dependency.py`)
//! and the immutable snapshot the result handler consults
//! (`adapters/site_connectivity.py:SiteConnectivitySnapshot`), PLAN.md
//! sections 6.9 and 6.10.

pub mod dependency;
pub mod snapshot;

pub use crate::env::SiteMode;
pub use dependency::{
    PathName, SiteDependency, resolve_site_dependency, warn_unobserved_requirements,
};
pub use snapshot::{HoldReason, PathSnapshot, PathState, SiteSnapshot};
