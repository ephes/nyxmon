//! SQLite store of the nyxmon agent (PLAN.md sections 2 and 3).
//!
//! One dedicated OS thread owns the only read-write connection (the "DB
//! actor"); [`Store`] is a cloneable handle that sends one closure per
//! operation and awaits the reply. Every operation is one transaction, like
//! the per-operation connections of `sqlite_repo.py`, and every write
//! transaction starts with `BEGIN IMMEDIATE` (section 3.3). The SQL of every
//! operation is copied from `sqlite_repo.py` (section 3.4); the store runs no
//! DDL and verifies the Django schema contract instead ([`schema`]).
//!
//! Where Python reads the clock or `check_batch_size()` inside an operation,
//! the Rust operation takes `now` / `batch_size` as parameters, so the caller
//! (and the tests) control them.
//!
//! The agent lock of section 3.6 lives in [`lock`].

mod actor;
mod checks;
mod cleaner;
mod error;
mod incidents;
pub mod lock;
mod results;
mod rows;
pub mod schema;

pub use actor::{Store, StoreOptions};
pub use error::StoreError;
pub use lock::{AgentLock, LockError, agent_lock_path};
pub use results::PersistRequest;
pub use rows::data_is_undecodable;
pub use schema::{MIN_NYXBOARD_MIGRATION, REQUIRED_COLUMNS, SchemaReport, check_schema};

/// Python logger name of `sqlite_repo.py`, used as the `tracing` target.
pub const LOGGER: &str = "nyxmon.adapters.repositories.sqlite_repo";
