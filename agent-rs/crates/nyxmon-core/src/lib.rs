//! Pure logic of the nyxmon agent: no I/O, no async runtime.
//!
//! Everything in this crate is deterministic and tested against fixtures
//! generated from the Python implementation (`agent-rs/fixtures`, PLAN.md
//! section 7.2).

#[cfg(test)]
mod fixture;
pub mod pyjson;

pub use pyjson::PyJson;
