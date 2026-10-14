//! The nyxmon agent runtime (PLAN.md sections 3.7, 5.1, 5.3, 5.8-5.10).
//!
//! - [`runner`]: executes a batch concurrently and hands the results over
//!   one at a time;
//! - [`results`]: `add_check_result` (suppression, decision, persistence,
//!   notification and acknowledgement);
//! - [`cleaner`]: the results cleaner;
//! - [`logging`]: the Python log format;
//! - [`collector`]: the claim/execute/sleep loop and its shutdown;
//! - [`site`]: the site connectivity observer runtime (probes, store and
//!   notifier wiring, the snapshot the result handler reads);
//! - [`incident_notification`]: the synthetic row of collector-scoped alerts;
//! - [`cli`], [`app`], [`startup`]: the command line, startup order and
//!   signal handling of the binary.

pub mod app;
pub mod cleaner;
pub mod cli;
pub mod collector;
pub mod incident_notification;
pub mod logging;
pub mod results;
pub mod runner;
pub mod site;
pub mod startup;
pub mod sync;
#[cfg(test)]
mod testutil;
