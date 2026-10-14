//! Check executors of the nyxmon agent and their network helpers.
//!
//! S1 provides the shared TLS helpers ([`tls`]); S5 the HTTP family
//! ([`http`], [`json_metrics`] on [`http_client`] and [`decode`]); S6-S7 add
//! the other executors (PLAN.md sections 5.5-5.7, 5.12-5.14).

pub mod ctx;
pub mod decode;
pub mod dns;
#[cfg(test)]
mod fake_server;
pub mod happy_eyeballs;
pub mod http;
pub mod http_client;
pub mod httpx_url;
pub mod idna2008;
pub mod json_metrics;
pub mod mail;
pub mod net;
pub mod ping;
pub mod pyidna;
pub mod pyos;
pub mod smtp;
pub mod starttls;
pub mod suppression_fetch;
pub mod tcp;
#[cfg(test)]
mod testutil;
pub mod tls;
