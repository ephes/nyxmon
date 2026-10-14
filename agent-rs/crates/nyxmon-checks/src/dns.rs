//! The `dns` check (`executors/dns_executor.py`, PLAN.md 5.6).
//!
//! [`wire`] holds the DNS wire format and the text forms as dnspython 2.8
//! produces them; the stub resolver and the executor follow in S5b part 2.

pub mod wire;
