//! The site connectivity observer runtime (`adapters/site_connectivity.py`
//! I/O parts, `bootstrap.py:_build_site_observer`,
//! `collector.py:site_incident_notifier` and
//! `_warn_unobserved_site_requirements`; PLAN.md section 6).
//!
//! The state machine is the pure [`Observer`] of `nyxmon-core`; this module
//! supplies its ports: the probe round ([`DefaultProbeRunner`]: TCP connects
//! to address literals, `getaddrinfo` for DNS names), the `site:connectivity`
//! row and the recheck queries on the [`Store`], the process notifier with
//! the synthetic incident row, the snapshot channel the result handler reads
//! ([`ObserverSiteState`]), and the loop that probes every `probe_interval`.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use nyxmon_checks::idna2008::idna2008_resolve;
use nyxmon_core::decision::SiteLimits;
use nyxmon_core::env::{ProbeKind, ProbeTarget, SiteEnvConfig, SiteMode};
use nyxmon_core::log::WarnOnce;
use nyxmon_core::model::HeldCheck;
use nyxmon_core::pyjson::PyDict;
use nyxmon_core::site::observer::SITE_INCIDENT_KEY;
use nyxmon_core::site::{
    LogLevel, Observer, PortError, ProbeOutcome, Raised, SiteDelivery, SiteMessage, SitePorts,
    SiteSnapshot,
};
use nyxmon_notify::Notifier;
use nyxmon_store::{Store, StoreError};
use tokio::sync::watch;
use tokio::task::JoinSet;

use crate::incident_notification::{
    IncidentNotification, build_incident_notification, result_status,
};
use crate::results::{Clock, SiteState};
use crate::sync::Flag;

macro_rules! site_log {
    ($level:ident, $($arg:tt)*) => {
        tracing::$level!(target: "nyxmon.adapters.site_connectivity", $($arg)*)
    };
}

/// The future of [`ProbeRunner::probe`].
pub type ProbeFuture<'a> = Pin<Box<dyn Future<Output = Result<bool, PortError>> + Send + 'a>>;

/// `ProbeRunner`: executes a single probe target. An `Err` is a raising
/// probe (logged at ERROR by the observer, counts as failed).
pub trait ProbeRunner: Send + Sync {
    fn probe<'a>(&'a self, target: &'a ProbeTarget) -> ProbeFuture<'a>;
}

/// `DefaultProbeRunner`: TCP connects for address literals, `getaddrinfo`
/// for DNS names, each bounded by the per-target timeout; never raises.
#[derive(Clone, Copy, Debug)]
pub struct DefaultProbeRunner {
    timeout: Duration,
}

impl DefaultProbeRunner {
    /// `DefaultProbeRunner(timeout=probe_timeout)`.
    pub fn new(timeout_seconds: i64) -> Self {
        DefaultProbeRunner {
            timeout: Duration::from_secs(u64::try_from(timeout_seconds).unwrap_or(0)),
        }
    }

    async fn attempt(target: &ProbeTarget) -> Result<bool, String> {
        match target.kind {
            // `anyio.getaddrinfo(host, None)`: a non-ASCII name is IDNA 2008
            // encoded first, as anyio does.
            ProbeKind::Dns => {
                let name = idna2008_resolve(&target.host).map_err(|err| err.to_string())?;
                let mut infos = tokio::net::lookup_host((name.as_str(), 0))
                    .await
                    .map_err(|err| err.to_string())?;
                Ok(infos.next().is_some())
            }
            // `anyio.connect_tcp(host, port)` then `aclose()`. The host is a
            // validated IP literal; a zone id (`fe80::1%eth0`) goes through
            // `getaddrinfo` like Python's socket module does.
            ProbeKind::Tcp => {
                let stream = tokio::net::TcpStream::connect((target.host.as_str(), target.port))
                    .await
                    .map_err(|err| err.to_string())?;
                drop(stream);
                Ok(true)
            }
        }
    }
}

impl ProbeRunner for DefaultProbeRunner {
    fn probe<'a>(&'a self, target: &'a ProbeTarget) -> ProbeFuture<'a> {
        Box::pin(async move {
            let outcome = match tokio::time::timeout(self.timeout, Self::attempt(target)).await {
                Ok(outcome) => outcome,
                Err(_) => Err("TimeoutError".to_owned()),
            };
            Ok(outcome.unwrap_or_else(|err| {
                site_log!(debug, "site connectivity probe failed for {target}\n{err}");
                false
            }))
        })
    }
}

fn port_error(err: StoreError) -> PortError {
    PortError(err.to_string())
}

/// What the result handler reads: the latest published snapshot and the
/// hold bounds of the observer's configuration (`_site_config`).
pub struct ObserverSiteState {
    snapshots: watch::Receiver<SiteSnapshot>,
    limits: SiteLimits,
}

impl SiteState for ObserverSiteState {
    fn snapshot(&self) -> Option<SiteSnapshot> {
        Some(self.snapshots.borrow().clone())
    }

    fn limits(&self) -> SiteLimits {
        self.limits
    }
}

/// The ports of the observer in the agent process.
pub struct AgentSitePorts {
    store: Store,
    notifier: Option<Arc<dyn Notifier>>,
    probes: Arc<dyn ProbeRunner>,
    round_budget: Duration,
    clock: Clock,
    snapshots: watch::Sender<SiteSnapshot>,
}

impl AgentSitePorts {
    /// `site_incident_notifier(process_notifier)` applied to one message:
    /// only a reported failure is undelivered; a raising notifier is logged
    /// and counts as undelivered.
    async fn send(notifier: &dyn Notifier, message: &SiteMessage) -> SiteDelivery {
        let (check, result) = build_incident_notification(&IncidentNotification {
            incident_key: message.incident_key,
            name: message.name,
            error_type: message.error_type,
            error_msg: &message.error_msg,
            status: result_status(message.status),
            opsgate_ticket: message.opsgate_ticket,
            alert_count: 0,
        });
        match notifier.notify_check_failed(&check, &result).await {
            Ok(delivery) if delivery.is_failure() => SiteDelivery::Failed,
            Ok(_) => SiteDelivery::Delivered,
            Err(err) => {
                tracing::error!(
                    target: "nyxmon.adapters.collector",
                    "failed to send the site connectivity notification for {}\n{err}",
                    message.incident_key
                );
                SiteDelivery::Failed
            }
        }
    }
}

impl SitePorts for AgentSitePorts {
    fn now(&self) -> i64 {
        (self.clock)()
    }

    /// `_probe_round`: every target concurrently inside `probe_timeout + 2`
    /// seconds; a target that did not finish counts as failed.
    async fn probe(&self, targets: &[ProbeTarget]) -> Vec<ProbeOutcome> {
        let mut outcomes = vec![ProbeOutcome::Failed; targets.len()];
        let mut round = JoinSet::new();
        for (index, target) in targets.iter().enumerate() {
            let probes = self.probes.clone();
            let target = target.clone();
            round.spawn(async move {
                let outcome = match probes.probe(&target).await {
                    Ok(true) => ProbeOutcome::Answered,
                    Ok(false) => ProbeOutcome::Failed,
                    Err(err) => ProbeOutcome::Raised(err),
                };
                (index, outcome)
            });
        }
        let _ = tokio::time::timeout(self.round_budget, async {
            while let Some(joined) = round.join_next().await {
                if let Ok((index, outcome)) = joined {
                    outcomes[index] = outcome;
                }
            }
        })
        .await;
        // Dropping the set cancels the targets still running.
        round.abort_all();
        outcomes
    }

    async fn get_incident(&self) -> Result<Option<PyDict>, PortError> {
        self.store
            .get_collector_incident(SITE_INCIDENT_KEY)
            .await
            .map(|incident| incident.map(|incident| incident.payload))
            .map_err(port_error)
    }

    async fn set_incident_payload(&self, payload: &PyDict) -> Result<bool, PortError> {
        self.store
            .set_collector_incident_payload(SITE_INCIDENT_KEY, payload.clone())
            .await
            .map(|incident| incident.is_some())
            .map_err(port_error)
    }

    async fn open_incident(&self, now: i64, payload: &PyDict) -> Result<(), PortError> {
        self.store
            .open_collector_incident(SITE_INCIDENT_KEY, now, payload.clone())
            .await
            .map(|_| ())
            .map_err(port_error)
    }

    async fn count_held_checks(&self) -> Result<i64, PortError> {
        self.store.count_held_checks().await.map_err(port_error)
    }

    async fn count_processing_claims_before(&self, epoch: i64) -> Result<i64, PortError> {
        self.store
            .count_processing_claims_before(epoch)
            .await
            .map_err(port_error)
    }

    async fn list_held_checks(&self) -> Result<Vec<HeldCheck>, PortError> {
        self.store.list_held_checks().await.map_err(port_error)
    }

    async fn reschedule_checks(&self, check_ids: &[i64], run_at: i64) -> Result<i64, PortError> {
        self.store
            .reschedule_checks(check_ids, run_at)
            .await
            .map(|count| i64::try_from(count).unwrap_or(i64::MAX))
            .map_err(port_error)
    }

    async fn notify(&self, message: &SiteMessage) -> Result<SiteDelivery, PortError> {
        let Some(notifier) = &self.notifier else {
            site_log!(error, "the site connectivity notifier is not configured");
            return Ok(SiteDelivery::Failed);
        };
        Ok(Self::send(notifier.as_ref(), message).await)
    }

    fn publish(&self, snapshot: &SiteSnapshot) {
        self.snapshots.send_replace(snapshot.clone());
    }

    fn log(&self, level: LogLevel, message: &str, error: Option<&PortError>) {
        let text = match error {
            Some(PortError(detail)) => format!("{message}\n{detail}"),
            None => message.to_owned(),
        };
        match level {
            LogLevel::Info => site_log!(info, "{text}"),
            LogLevel::Warning => site_log!(warn, "{text}"),
            LogLevel::Error => site_log!(error, "{text}"),
        }
    }
}

/// `SiteConnectivityObserver` with its I/O: one per process when the mode
/// is not `off`.
pub struct SiteRuntime {
    observer: Observer,
    ports: AgentSitePorts,
    store: Store,
}

impl std::fmt::Debug for SiteRuntime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SiteRuntime")
            .field("observer", &self.observer)
            .finish_non_exhaustive()
    }
}

impl SiteRuntime {
    /// `_build_site_observer`: the store is both the incident store and the
    /// scheduler of the recovery recheck; `notifier` is the process notifier
    /// (`None`: every send fails with `the site connectivity notifier is not
    /// configured`). `warnings` is the process-wide warn-once state shared
    /// with the result handler.
    pub fn new(
        config: SiteEnvConfig,
        warnings: Arc<WarnOnce>,
        store: Store,
        notifier: Option<Arc<dyn Notifier>>,
        clock: Clock,
    ) -> Self {
        let probes = Arc::new(DefaultProbeRunner::new(config.probe_timeout));
        Self::with_probes(config, warnings, store, notifier, clock, probes)
    }

    /// [`SiteRuntime::new`] with an injected probe implementation (tests).
    pub fn with_probes(
        config: SiteEnvConfig,
        warnings: Arc<WarnOnce>,
        store: Store,
        notifier: Option<Arc<dyn Notifier>>,
        clock: Clock,
        probes: Arc<dyn ProbeRunner>,
    ) -> Self {
        let round_budget =
            Duration::from_secs(u64::try_from(config.probe_round_budget_seconds()).unwrap_or(0));
        let observer = Observer::new(config, warnings);
        // `__init__` publishes the first snapshot.
        let (snapshots, _) = watch::channel(observer.snapshot().clone());
        SiteRuntime {
            observer,
            ports: AgentSitePorts {
                store: store.clone(),
                notifier,
                probes,
                round_budget,
                clock,
                snapshots,
            },
            store,
        }
    }

    /// The handler's view of this observer (`site_state`).
    pub fn state(&self) -> Arc<dyn SiteState> {
        Arc::new(ObserverSiteState {
            snapshots: self.ports.snapshots.subscribe(),
            limits: SiteLimits::from_config(self.observer.config()),
        })
    }

    pub fn observer(&self) -> &Observer {
        &self.observer
    }

    /// The most recently published snapshot.
    pub fn snapshot(&self) -> SiteSnapshot {
        self.ports.snapshots.borrow().clone()
    }

    /// `restore`.
    ///
    /// # Errors
    /// The Python exception a malformed persisted timestamp raises.
    pub async fn restore(&mut self) -> Result<(), Raised> {
        self.observer.restore(&self.ports).await
    }

    /// `run_once`.
    ///
    /// # Errors
    /// The Python exception a malformed persisted value raises mid-round.
    pub async fn run_once(&mut self) -> Result<(), Raised> {
        self.observer.run_once(&self.ports).await
    }

    /// `collector.py:_warn_unobserved_site_requirements`: warn once per
    /// check whose single-path requirement nobody observes.
    pub async fn warn_unobserved_site_requirements(&self) {
        match self.store.list_checks().await {
            Ok(checks) => self.observer.warn_unobserved_requirements(
                checks.iter().map(|check| (check.check_id, &check.data)),
            ),
            Err(err) => tracing::error!(
                target: "nyxmon.adapters.collector",
                "failed to check site dependencies against the probes\n{err}"
            ),
        }
    }

    /// The observer task: the unobserved-requirement warning of the
    /// collector's first iteration, then `run`: restore, and a round every
    /// `probe_interval` until `stop` is set. A stop takes effect after the
    /// current round or sleep; the shutdown aborts the task once the
    /// collector is done (Python cancels the portal).
    pub async fn run(mut self, stop: Arc<Flag>) {
        if self.observer.config().mode == SiteMode::Off {
            return;
        }
        self.warn_unobserved_site_requirements().await;
        if let Err(err) = self.restore().await {
            // Python's `run` lets the exception end the observer task
            // silently; the port logs it (P3, PLAN.md D-19).
            site_log!(
                error,
                "site connectivity observer stopped: restoring the persisted lifecycle failed\n{err}"
            );
            return;
        }
        let interval =
            Duration::from_secs(u64::try_from(self.observer.config().probe_interval).unwrap_or(0));
        while !stop.is_set() {
            if let Err(err) = self.run_once().await {
                site_log!(error, "site connectivity round failed\n{err}");
            }
            if stop.is_set() {
                break;
            }
            tokio::time::sleep(interval).await;
        }
    }
}

#[cfg(test)]
mod holds_tests;
#[cfg(test)]
mod tests;
