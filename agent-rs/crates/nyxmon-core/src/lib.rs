//! Pure logic of the nyxmon agent: no I/O, no async runtime.
//!
//! Everything in this crate is deterministic and tested against fixtures
//! generated from the Python implementation (`agent-rs/fixtures`, PLAN.md
//! section 7.2).

#[cfg(test)]
mod alerting_tests;
pub mod config;
pub mod decision;
pub mod env;
#[cfg(test)]
mod fixture;
pub mod incident;
pub mod lease;
pub mod log;
pub mod metrics_eval;
pub mod model;
pub mod ping_text;
pub mod policy;
pub mod pyjson;
pub mod pyurl;
pub mod site;
pub mod suppression;

pub use pyjson::PyJson;
