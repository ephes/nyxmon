//! SQLite store of the nyxmon agent.
//!
//! Slice S2 fills this crate (PLAN.md sections 2, 3): the rusqlite actor on a
//! dedicated thread, schema verification against the Django schema contract,
//! and every claim/result/incident/cleaner operation of section 3.4.
//! S1 only creates the crate so parallel slices add modules, not workspace
//! members.
