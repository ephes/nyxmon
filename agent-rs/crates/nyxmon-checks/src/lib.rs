//! Check executors of the nyxmon agent and their network helpers.
//!
//! S1 provides the shared TLS helpers ([`tls`]); slices S5-S7 add the
//! executors (PLAN.md sections 5.5-5.7, 5.12-5.14) and the registry.

pub mod happy_eyeballs;
pub mod idna2008;
pub mod pyidna;
pub mod pyos;
pub mod starttls;
pub mod tcp;
pub mod tls;
