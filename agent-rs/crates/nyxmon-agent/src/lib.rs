//! The nyxmon agent runtime (PLAN.md sections 3.7, 5.1, 5.3, 5.8-5.10).
//!
//! - [`runner`]: executes a batch concurrently and hands the results over
//!   one at a time;
//! - [`results`]: `add_check_result` (suppression, decision, persistence,
//!   notification and acknowledgement);
//! - [`cleaner`]: the results cleaner;
//! - [`logging`]: the Python log format.

pub mod cleaner;
pub mod logging;
pub mod results;
pub mod runner;
pub mod sync;
#[cfg(test)]
mod testutil;
