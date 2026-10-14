//! The site connectivity observer as a pure state machine
//! (`site_connectivity.py:SiteConnectivityObserver`), PLAN.md sections
//! 6.3-6.8.
//!
//! Everything with side effects goes through [`SitePorts`]: the clock, the
//! probe round, the `site:connectivity` incident row, the scheduler queries
//! of the recovery recheck, the notifier, snapshot publication and logging.
//! The control flow is a line-by-line port of the Python methods, so a call
//! sequence against the ports is the same as the Python observer's against
//! its store, scheduler and notifier; the replay fixtures pin that sequence.
//! The I/O driver (probe runner, store and notifier wiring, the loop) lives
//! in `nyxmon-agent` (slice S11).

use std::collections::HashSet;
use std::future::Future;
use std::sync::Arc;

use super::dependency::{PathName, resolve_site_dependency, warn_unobserved_requirements};
use super::render::{
    Raised, SITE_DELIVERY_RETRY_SECONDS, SITE_SUMMARY_MAX_CHARS, SITE_SUMMARY_MAX_RECORDS, as_int,
    duration, iso, record_is_due, summary_line,
};
use super::snapshot::{PathSnapshot, PathState, SiteSnapshot};
use crate::env::{ProbeTarget, SiteEnvConfig, SiteMode};
use crate::log::{WarnOnce, loggers};
use crate::model::HeldCheck;
use crate::pyjson::{CmpOp, PyDict, PyJson, char_len, py_cmp, py_repr, py_truthy};

/// `SITE_INCIDENT_KEY`: the one row that carries the whole site lifecycle.
pub const SITE_INCIDENT_KEY: &str = "site:connectivity";
/// `SITE_INCIDENT_NAME`: name of the synthetic check row of site messages.
pub const SITE_INCIDENT_NAME: &str = "site connectivity";
/// `SITE_INCIDENT_TYPE`: `payload["incident_type"]`.
pub const SITE_INCIDENT_TYPE: &str = "site_connectivity";
/// `SITE_PAYLOAD_VERSION`.
pub const SITE_PAYLOAD_VERSION: i64 = 1;
/// `SITE_ONGOING_ERROR_TYPE`.
pub const SITE_ONGOING_ERROR_TYPE: &str = "site_connectivity_outage";
/// `SITE_SUMMARY_ERROR_TYPE`.
pub const SITE_SUMMARY_ERROR_TYPE: &str = "site_connectivity_summary";

/// Logger of every observer line (`nyxmon.adapters.site_connectivity`).
pub const LOGGER: &str = loggers::SITE_CONNECTIVITY;

/// `PHASE_IDLE` / `PHASE_ACTIVE`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Idle,
    Active,
}

impl Phase {
    pub fn as_str(self) -> &'static str {
        match self {
            Phase::Idle => "idle",
            Phase::Active => "active",
        }
    }
}

/// Level of an observer log line. `Error` lines are Python
/// `logger.exception`/`logger.error` calls; the port appends the error
/// detail (Python's traceback) when one is given.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LogLevel {
    Info,
    Warning,
    Error,
}

impl LogLevel {
    /// The Python level name.
    pub fn as_str(self) -> &'static str {
        match self {
            LogLevel::Info => "INFO",
            LogLevel::Warning => "WARNING",
            LogLevel::Error => "ERROR",
        }
    }
}

/// A failed port call (a Python exception raised by the store, scheduler or
/// notifier). The text is diagnostic only; the logged message is fixed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PortError(pub String);

/// Outcome of probing one target.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProbeOutcome {
    /// The target answered inside the per-target timeout.
    Answered,
    /// It did not answer, or the round budget ran out first (not logged).
    Failed,
    /// The probe itself raised (logged at ERROR, counts as failed).
    Raised(PortError),
}

/// What the notifier reports (`True`/`False`/`None`); only `Failed` counts
/// as undelivered.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SiteDelivery {
    Delivered,
    Failed,
    Unknown,
}

/// The arguments of the `SiteNotifier` call.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SiteMessage {
    pub incident_key: &'static str,
    pub name: &'static str,
    pub error_type: &'static str,
    pub error_msg: String,
    pub status: &'static str,
    pub opsgate_ticket: bool,
}

/// Everything the observer does to the outside world.
///
/// Store methods address the [`SITE_INCIDENT_KEY`] row. Errors are logged
/// by the observer and never escape a round.
pub trait SitePorts: Sync {
    /// The current epoch (`int(clock())`).
    fn now(&self) -> i64;

    /// Probe every target concurrently; one outcome per target, in order.
    /// Missing outcomes count as failed.
    fn probe(&self, targets: &[ProbeTarget]) -> impl Future<Output = Vec<ProbeOutcome>> + Send;

    /// `get_collector_incident`: the row's payload, `None` without a row.
    fn get_incident(&self) -> impl Future<Output = Result<Option<PyDict>, PortError>> + Send;

    /// `set_collector_incident_payload`: `false` when there is no row.
    fn set_incident_payload(
        &self,
        payload: &PyDict,
    ) -> impl Future<Output = Result<bool, PortError>> + Send;

    /// `open_collector_incident`: create the row silently or replace its
    /// payload.
    fn open_incident(
        &self,
        now: i64,
        payload: &PyDict,
    ) -> impl Future<Output = Result<(), PortError>> + Send;

    /// `count_held_checks_async`.
    fn count_held_checks(&self) -> impl Future<Output = Result<i64, PortError>> + Send;

    /// `count_processing_claims_before_async`.
    fn count_processing_claims_before(
        &self,
        epoch: i64,
    ) -> impl Future<Output = Result<i64, PortError>> + Send;

    /// `list_held_checks_async`.
    fn list_held_checks(&self) -> impl Future<Output = Result<Vec<HeldCheck>, PortError>> + Send;

    /// `reschedule_checks_async(check_ids, run_at=...)`.
    fn reschedule_checks(
        &self,
        check_ids: &[i64],
        run_at: i64,
    ) -> impl Future<Output = Result<i64, PortError>> + Send;

    /// The site notifier.
    fn notify(
        &self,
        message: &SiteMessage,
    ) -> impl Future<Output = Result<SiteDelivery, PortError>> + Send;

    /// A new snapshot was published (`_publish_snapshot`).
    fn publish(&self, snapshot: &SiteSnapshot);

    /// One log line of [`LOGGER`].
    fn log(&self, level: LogLevel, message: &str, error: Option<&PortError>);
}

/// `_PathRuntime`.
#[derive(Clone, Debug)]
struct PathRuntime {
    name: PathName,
    state: PathState,
    down_since: i64,
    recovered_at: i64,
    release_at: i64,
    last_release_at: i64,
    failures: i64,
}

impl PathRuntime {
    fn new(name: PathName, state: PathState) -> Self {
        PathRuntime {
            name,
            state,
            down_since: 0,
            recovered_at: 0,
            release_at: 0,
            last_release_at: 0,
            failures: 0,
        }
    }

    fn observed(&self) -> bool {
        self.state != PathState::Unobserved
    }
}

/// Python `a == b` for a persisted value (`None` when missing).
fn py_eq(value: Option<&PyJson>, other: &PyJson) -> bool {
    py_cmp(CmpOp::Eq, value.unwrap_or(&PyJson::Null), other).unwrap_or(false)
}

/// Python truthiness of a persisted value (`None` when missing).
fn truthy(value: Option<&PyJson>) -> bool {
    value.is_some_and(py_truthy)
}

fn dict_of(value: Option<&PyJson>) -> PyDict {
    match value {
        Some(PyJson::Dict(map)) => map.clone(),
        _ => PyDict::new(),
    }
}

/// `SiteConnectivityObserver` without its I/O.
pub struct Observer {
    config: SiteEnvConfig,
    warnings: Arc<WarnOnce>,
    paths: [PathRuntime; 3],
    observed_at: i64,
    phase: Phase,
    incident_id: i64,
    ongoing: PyDict,
    summaries: Vec<PyDict>,
    recheck: PyDict,
    snapshot: SiteSnapshot,
    row_present: bool,
    /// Until the row was read once the observer must never write over it.
    restored: bool,
}

impl std::fmt::Debug for Observer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Observer")
            .field("phase", &self.phase)
            .field("incident_id", &self.incident_id)
            .field("snapshot", &self.snapshot)
            .finish_non_exhaustive()
    }
}

impl Observer {
    /// `SiteConnectivityObserver.__init__`: paths without targets are
    /// `unobserved`, the others `up`; the first snapshot is built at once.
    /// `warnings` is the process-wide warn-once state shared with the
    /// result handler (`site_dependency` warnings of the recheck).
    pub fn new(config: SiteEnvConfig, warnings: Arc<WarnOnce>) -> Self {
        let paths = PathName::ALL.map(|name| {
            let state = if config.targets_for(name).is_empty() {
                PathState::Unobserved
            } else {
                PathState::Up
            };
            PathRuntime::new(name, state)
        });
        let mut observer = Observer {
            snapshot: SiteSnapshot {
                paths: [None; 3],
                observed_at: 0,
                mode: config.mode,
                incident_id: 0,
            },
            config,
            warnings,
            paths,
            observed_at: 0,
            phase: Phase::Idle,
            incident_id: 0,
            ongoing: PyDict::new(),
            summaries: Vec::new(),
            recheck: PyDict::new(),
            row_present: false,
            restored: false,
        };
        observer.snapshot = observer.build_snapshot();
        observer
    }

    pub fn config(&self) -> &SiteEnvConfig {
        &self.config
    }

    /// The most recently published snapshot.
    pub fn snapshot(&self) -> &SiteSnapshot {
        &self.snapshot
    }

    /// Whether the persisted row was read successfully once.
    pub fn is_restored(&self) -> bool {
        self.restored
    }

    /// `unobserved_paths`.
    pub fn unobserved_paths(&self) -> Vec<PathName> {
        self.paths
            .iter()
            .filter(|path| !path.observed())
            .map(|path| path.name)
            .collect()
    }

    /// `SiteConnectivityObserver.warn_unobserved_requirements`; `checks`
    /// yields `(check_id, data)`.
    pub fn warn_unobserved_requirements<'a>(
        &self,
        checks: impl IntoIterator<Item = (i64, &'a PyJson)>,
    ) {
        warn_unobserved_requirements(&self.warnings, checks, &self.unobserved_paths());
    }

    // ------------------------------------------------------------ restore

    /// `restore`: load the whole lifecycle from the persisted row.
    ///
    /// A failed read leaves the observer unrestored (nothing is written
    /// until a later read succeeds); an absent row counts as restored.
    ///
    /// # Errors
    ///
    /// The Python exception a malformed timestamp raises (`int(inf)`), with
    /// the fields restored before it kept, exactly as in Python.
    pub async fn restore<P: SitePorts>(&mut self, ports: &P) -> Result<(), Raised> {
        if self.config.mode == SiteMode::Off {
            return Ok(());
        }
        let incident = match ports.get_incident().await {
            Ok(incident) => incident,
            Err(error) => {
                ports.log(
                    LogLevel::Error,
                    "failed to restore the site connectivity incident",
                    Some(&error),
                );
                return Ok(());
            }
        };
        self.restored = true;
        let Some(payload) = incident else {
            self.publish(ports);
            return Ok(());
        };
        self.row_present = true;
        let current = PyJson::Int(SITE_PAYLOAD_VERSION);
        let version = payload.get("version").unwrap_or(&current);
        if !py_eq(Some(version), &current) {
            ports.log(
                LogLevel::Warning,
                &format!(
                    "ignoring the site connectivity payload: unknown version {}",
                    py_repr(version)
                ),
                None,
            );
            self.publish(ports);
            return Ok(());
        }

        if let Some(PyJson::Dict(raw_paths)) = payload.get("paths") {
            for (name, raw) in raw_paths {
                let Some(name) = PathName::parse(name) else {
                    continue;
                };
                let path = &mut self.paths[name.index()];
                if !path.observed() {
                    continue;
                }
                let PyJson::Dict(raw) = raw else {
                    continue;
                };
                let state = match raw.get("state") {
                    Some(PyJson::Str(state)) => PathState::parse(state),
                    _ => None,
                };
                let Some(state) = state.filter(|state| *state != PathState::Unobserved) else {
                    continue;
                };
                path.state = state;
                path.down_since = as_int(raw.get("down_since"))?;
                path.recovered_at = as_int(raw.get("recovered_at"))?;
                path.release_at = as_int(raw.get("release_at"))?;
                path.last_release_at = as_int(raw.get("last_release_at"))?;
                // Derived, not persisted: a restored `failing` path carries
                // the one failure its state implies.
                path.failures = i64::from(state == PathState::Failing);
            }
        }

        self.observed_at = as_int(payload.get("observed_at"))?;
        self.phase = match payload.get("phase") {
            Some(PyJson::Str(phase)) if phase == "active" => Phase::Active,
            _ => Phase::Idle,
        };
        self.incident_id = as_int(payload.get("incident_id"))?;
        self.ongoing = dict_of(payload.get("ongoing"));
        self.summaries = match payload.get("summaries") {
            Some(PyJson::List(records)) => records
                .iter()
                .filter_map(|record| record.as_dict().cloned())
                .collect(),
            _ => Vec::new(),
        };
        self.recheck = dict_of(payload.get("recheck"));

        let now = ports.now();
        if self.phase == Phase::Active && !self.is_fresh(now) {
            let message = format!(
                "site connectivity state observed at {} is stale; the active outage {} is set to \
                 idle, keeping release watermarks, pending summaries and the recheck item",
                iso(self.observed_at)?,
                self.incident_id
            );
            ports.log(LogLevel::Warning, &message, None);
            self.retire_ongoing();
            for path in self.paths.iter_mut().filter(|path| path.observed()) {
                path.state = PathState::Up;
                path.down_since = 0;
                path.recovered_at = 0;
                path.release_at = 0;
                path.failures = 0;
            }
            self.phase = Phase::Idle;
            self.incident_id = 0;
        }
        self.publish(ports);
        Ok(())
    }

    // -------------------------------------------------------------- round

    /// `run_once`: restore if needed, probe, advance paths and phase,
    /// publish, persist, then ongoing alert, summaries and recheck.
    ///
    /// # Errors
    ///
    /// The Python exception a malformed persisted value raises mid-round
    /// (the I/O driver logs `site connectivity round failed`).
    pub async fn run_once<P: SitePorts>(&mut self, ports: &P) -> Result<(), Raised> {
        if self.config.mode == SiteMode::Off {
            return Ok(());
        }
        if !self.restored {
            self.restore(ports).await?;
            if !self.restored {
                ports.log(
                    LogLevel::Warning,
                    "skipping the site connectivity round: the persisted lifecycle could not be \
                     restored yet",
                    None,
                );
                return Ok(());
            }
        }
        let now = ports.now();
        let failed = self.probe_round(ports).await;
        let released = self.advance_paths(&failed, now);
        self.observed_at = now;
        self.advance_incident(ports, now, &released).await?;
        self.publish(ports);
        self.write_payload(ports, now).await;
        self.process_ongoing(ports, now).await?;
        self.process_summaries(ports, now).await?;
        self.process_recheck(ports, now).await
    }

    /// `_probe_round`: per observed path whether the round failed (every
    /// target failed).
    async fn probe_round<P: SitePorts>(&self, ports: &P) -> [Option<bool>; 3] {
        let targets = self.config.all_targets();
        if targets.is_empty() {
            return [None; 3];
        }
        let outcomes = ports.probe(&targets).await;
        let mut answered = vec![false; targets.len()];
        for (index, target) in targets.iter().enumerate() {
            match outcomes.get(index) {
                Some(ProbeOutcome::Answered) => answered[index] = true,
                Some(ProbeOutcome::Raised(error)) => ports.log(
                    LogLevel::Error,
                    &format!("site connectivity probe raised for {target}"),
                    Some(error),
                ),
                Some(ProbeOutcome::Failed) | None => {}
            }
        }
        let mut failed = [None; 3];
        let mut offset = 0;
        for name in PathName::ALL {
            let count = self.config.targets_for(name).len();
            if count > 0 {
                failed[name.index()] = Some(!answered[offset..offset + count].iter().any(|ok| *ok));
            }
            offset += count;
        }
        failed
    }

    /// `_advance_paths`: apply the round's outcome first, then release
    /// recovering paths whose grace ended. Returns `(path, release)` pairs.
    fn advance_paths(&mut self, failed: &[Option<bool>; 3], now: i64) -> Vec<(PathName, i64)> {
        let mut released = Vec::new();
        for path in self.paths.iter_mut().filter(|path| path.observed()) {
            if let Some(failed) = failed[path.name.index()] {
                Self::apply_outcome(&self.config, path, failed, now);
            }
            if path.state == PathState::Recovering && now >= path.release_at {
                path.last_release_at = path.release_at;
                path.state = PathState::Up;
                path.release_at = 0;
                path.failures = 0;
                released.push((path.name, path.last_release_at));
            }
        }
        released
    }

    /// `_apply_outcome`.
    fn apply_outcome(config: &SiteEnvConfig, path: &mut PathRuntime, failed: bool, now: i64) {
        if failed {
            match path.state {
                PathState::Up | PathState::Failing => {
                    path.failures = path.failures.saturating_add(1);
                    path.state = PathState::Failing;
                    if path.failures >= config.down_after_failures {
                        path.state = PathState::Down;
                        path.down_since = now;
                        path.recovered_at = 0;
                        path.release_at = 0;
                        path.failures = 0;
                    }
                }
                PathState::Recovering => {
                    // No re-confirmation: one failed round returns the path
                    // to `down` and keeps `down_since`.
                    path.state = PathState::Down;
                    path.recovered_at = 0;
                    path.release_at = 0;
                }
                PathState::Down | PathState::Unobserved => {}
            }
            return;
        }
        path.failures = 0;
        match path.state {
            PathState::Failing => path.state = PathState::Up,
            PathState::Down => {
                path.state = PathState::Recovering;
                path.recovered_at = now;
                path.release_at = now.saturating_add(config.recovery_grace_seconds);
            }
            _ => {}
        }
    }

    fn observed_paths(&self) -> impl Iterator<Item = &PathRuntime> {
        self.paths.iter().filter(|path| path.observed())
    }

    fn down_paths(&self) -> Vec<&PathRuntime> {
        self.observed_paths()
            .filter(|path| path.state == PathState::Down)
            .collect()
    }

    // ---------------------------------------------------- incident phase

    /// `_advance_incident`.
    async fn advance_incident<P: SitePorts>(
        &mut self,
        ports: &P,
        now: i64,
        released: &[(PathName, i64)],
    ) -> Result<(), Raised> {
        let down = self.down_paths();
        let active = self.observed_paths().any(|path| path.state.is_blocking());

        if self.phase == Phase::Idle {
            if let Some(incident_id) = down.iter().map(|path| path.down_since).min() {
                let mut names: Vec<&str> = down.iter().map(|path| path.name.as_str()).collect();
                names.sort_unstable();
                let message = format!(
                    "site connectivity incident {incident_id} opened: {} down",
                    names.join(", ")
                );
                self.phase = Phase::Active;
                self.incident_id = incident_id;
                ports.log(LogLevel::Info, &message, None);
            }
            return Ok(());
        }

        if down.is_empty() {
            // The last `down` path became `recovering`: an undelivered
            // ongoing alert must never be sent afterwards.
            self.retire_ongoing();
        }

        if active {
            if let Some(release) = released.iter().map(|(_, release)| *release).max() {
                self.open_recheck(release)?;
            }
            return Ok(());
        }

        self.close_incident(ports, now, released).await
    }

    /// `_retire_ongoing`.
    fn retire_ongoing(&mut self) {
        if self.ongoing.is_empty()
            || !py_eq(
                self.ongoing.get("incident_id"),
                &PyJson::Int(self.incident_id),
            )
        {
            return;
        }
        if truthy(self.ongoing.get("delivered")) || truthy(self.ongoing.get("retired")) {
            return;
        }
        self.ongoing
            .insert("retired".to_owned(), PyJson::Bool(true));
    }

    /// `_open_recheck`: open the recheck item or extend its release.
    fn open_recheck(&mut self, release_at: i64) -> Result<(), Raised> {
        let mut release_at = release_at;
        if truthy(self.recheck.get("pending")) {
            release_at = release_at.max(as_int(self.recheck.get("release_at"))?);
        }
        let mut recheck = PyDict::new();
        recheck.insert("pending".to_owned(), PyJson::Bool(true));
        recheck.insert("release_at".to_owned(), PyJson::Int(release_at));
        self.recheck = recheck;
        Ok(())
    }

    /// `_close_incident`: append the summary record once per incident, open
    /// the recheck, return to `idle`.
    async fn close_incident<P: SitePorts>(
        &mut self,
        ports: &P,
        now: i64,
        released: &[(PathName, i64)],
    ) -> Result<(), Raised> {
        let incident_id = self.incident_id;
        let participants: Vec<PathRuntime> = self
            .observed_paths()
            .filter(|path| {
                path.down_since >= incident_id && incident_id > 0 && path.recovered_at > 0
            })
            .cloned()
            .collect();
        let last_recovered_at = participants
            .iter()
            .map(|path| path.recovered_at)
            .max()
            .unwrap_or(now);
        let lasted = last_recovered_at.saturating_sub(incident_id).max(0);
        let incident = PyJson::Int(incident_id);
        let delivered = truthy(self.ongoing.get("delivered"))
            && py_eq(self.ongoing.get("incident_id"), &incident);
        let eligible = lasted >= self.config.incident_notify_after_seconds || delivered;
        let already = self
            .summaries
            .iter()
            .any(|record| py_eq(record.get("incident_id"), &incident));
        if eligible && !already {
            let mut paths = PyDict::new();
            for path in &participants {
                let mut entry = PyDict::new();
                entry.insert("down_since".to_owned(), PyJson::Int(path.down_since));
                entry.insert("recovered_at".to_owned(), PyJson::Int(path.recovered_at));
                paths.insert(path.name.as_str().to_owned(), PyJson::Dict(entry));
            }
            let held_at_close = match ports.count_held_checks().await {
                Ok(count) => count,
                Err(error) => {
                    ports.log(LogLevel::Error, "failed to count held checks", Some(&error));
                    0
                }
            };
            let mut record = PyDict::new();
            record.insert("incident_id".to_owned(), PyJson::Int(incident_id));
            record.insert("started_at".to_owned(), PyJson::Int(incident_id));
            record.insert("ended_at".to_owned(), PyJson::Int(last_recovered_at));
            record.insert("paths".to_owned(), PyJson::Dict(paths));
            record.insert("ongoing_delivered".to_owned(), PyJson::Bool(delivered));
            record.insert("held_at_close".to_owned(), PyJson::Int(held_at_close));
            record.insert("attempt_at".to_owned(), PyJson::Int(0));
            record.insert("attempts".to_owned(), PyJson::Int(0));
            self.summaries.push(record);
        }
        let release_at = released
            .iter()
            .map(|(_, release)| *release)
            .max()
            .or_else(|| participants.iter().map(|path| path.last_release_at).max())
            .unwrap_or(now);
        self.open_recheck(release_at)?;
        ports.log(
            LogLevel::Info,
            &format!(
                "site connectivity incident {incident_id} closed after {} (summary: {})",
                duration(lasted),
                if eligible { "yes" } else { "no" }
            ),
            None,
        );
        self.phase = Phase::Idle;
        self.incident_id = 0;
        Ok(())
    }

    // ------------------------------------------------------------- alerts

    /// `_process_ongoing`: the ongoing outage alert, intent written first.
    async fn process_ongoing<P: SitePorts>(&mut self, ports: &P, now: i64) -> Result<(), Raised> {
        if self.phase != Phase::Active {
            return Ok(());
        }
        if self.down_paths().is_empty() {
            return Ok(());
        }
        if now.saturating_sub(self.incident_id) < self.config.incident_notify_after_seconds {
            return Ok(());
        }
        let same_incident = py_eq(
            self.ongoing.get("incident_id"),
            &PyJson::Int(self.incident_id),
        );
        let last_alert_at = if same_incident {
            as_int(self.ongoing.get("last_alert_at"))?
        } else {
            0
        };
        let attempts = if same_incident {
            as_int(self.ongoing.get("attempts"))?
        } else {
            0
        };
        let reminder = self.config.incident_reminder_seconds;
        if same_incident {
            if truthy(self.ongoing.get("delivered")) {
                if now.saturating_sub(last_alert_at) < reminder {
                    return Ok(());
                }
            } else if truthy(self.ongoing.get("retired")) {
                // A retired intent is never retried, but a path that flaps
                // back to `down` earns a new attempt once the reminder has
                // elapsed since the later of last attempt and last delivery.
                let reference = last_alert_at.max(as_int(self.ongoing.get("attempt_at"))?);
                if now.saturating_sub(reference) < reminder {
                    return Ok(());
                }
            } else if now.saturating_sub(as_int(self.ongoing.get("attempt_at"))?)
                < SITE_DELIVERY_RETRY_SECONDS
            {
                return Ok(());
            }
        }

        let mut ongoing = PyDict::new();
        ongoing.insert("incident_id".to_owned(), PyJson::Int(self.incident_id));
        ongoing.insert("attempt_at".to_owned(), PyJson::Int(now));
        ongoing.insert(
            "attempts".to_owned(),
            PyJson::Int(attempts.saturating_add(1)),
        );
        ongoing.insert("delivered".to_owned(), PyJson::Bool(false));
        ongoing.insert("retired".to_owned(), PyJson::Bool(false));
        ongoing.insert("last_alert_at".to_owned(), PyJson::Int(last_alert_at));
        self.ongoing = ongoing;
        if !self.write_payload(ports, now).await {
            // The intent must be durable before the send.
            return Ok(());
        }
        let message = self.ongoing_message(now)?;
        if !self
            .send(ports, SITE_ONGOING_ERROR_TYPE, message, "error", true)
            .await
        {
            return Ok(());
        }
        self.ongoing
            .insert("delivered".to_owned(), PyJson::Bool(true));
        self.ongoing
            .insert("last_alert_at".to_owned(), PyJson::Int(now));
        self.write_payload(ports, now).await;
        Ok(())
    }

    /// `_ongoing_message`.
    fn ongoing_message(&self, now: i64) -> Result<String, Raised> {
        let mut down = self.down_paths();
        down.sort_by_key(|path| path.name.as_str());
        let mut parts = Vec::with_capacity(down.len());
        for path in down {
            parts.push(format!(
                "{} down since {} ({})",
                path.name,
                iso(path.down_since)?,
                duration(now.saturating_sub(path.down_since))
            ));
        }
        Ok(format!(
            "Site connectivity outage {}: {}. Alerts of dependent checks are held for at most {}.",
            self.incident_id,
            parts.join(", "),
            duration(self.config.max_hold_seconds)
        ))
    }

    /// `_process_summaries`: send one size-bounded batch of due records.
    async fn process_summaries<P: SitePorts>(&mut self, ports: &P, now: i64) -> Result<(), Raised> {
        if self.summaries.is_empty() {
            return Ok(());
        }
        let (batch, message) = self.summary_batch(now)?;
        if batch.is_empty() {
            return Ok(());
        }
        let mut covered = HashSet::new();
        for index in batch {
            covered.insert(as_int(self.summaries[index].get("incident_id"))?);
        }
        let mut marked = Vec::with_capacity(self.summaries.len());
        for record in &self.summaries {
            if covered.contains(&as_int(record.get("incident_id"))?) {
                let mut record = record.clone();
                let attempts = as_int(record.get("attempts"))?;
                record.insert("attempt_at".to_owned(), PyJson::Int(now));
                record.insert(
                    "attempts".to_owned(),
                    PyJson::Int(attempts.saturating_add(1)),
                );
                marked.push(record);
            } else {
                marked.push(record.clone());
            }
        }
        self.summaries = marked;
        if !self.write_payload(ports, now).await {
            return Ok(());
        }
        if !self
            .send(ports, SITE_SUMMARY_ERROR_TYPE, message, "warning", false)
            .await
        {
            return Ok(());
        }
        // Only the records this send covered are removed.
        let mut kept = Vec::with_capacity(self.summaries.len());
        for record in &self.summaries {
            if !covered.contains(&as_int(record.get("incident_id"))?) {
                kept.push(record.clone());
            }
        }
        self.summaries = kept;
        self.write_payload(ports, now).await;
        Ok(())
    }

    /// `_summary_batch`: indices of the oldest due records that fit one
    /// message, and the message body.
    fn summary_batch(&self, now: i64) -> Result<(Vec<usize>, String), Raised> {
        let mut keys = Vec::with_capacity(self.summaries.len());
        for (index, record) in self.summaries.iter().enumerate() {
            let started_at = as_int(record.get("started_at"))?;
            let incident_id = as_int(record.get("incident_id"))?;
            keys.push(((started_at, incident_id), index));
        }
        // Stable, like Python's `sorted`.
        keys.sort_by_key(|(key, _)| *key);
        let mut batch = Vec::new();
        let mut lines = Vec::new();
        let mut used = 0usize;
        for (_, index) in keys {
            let record = &self.summaries[index];
            if !record_is_due(record, now)? {
                break;
            }
            if batch.len() >= SITE_SUMMARY_MAX_RECORDS {
                break;
            }
            let line = summary_line(record, SITE_SUMMARY_MAX_CHARS)?;
            let separator = usize::from(!batch.is_empty());
            let length = char_len(&line);
            if !batch.is_empty() && used + separator + length > SITE_SUMMARY_MAX_CHARS {
                break;
            }
            batch.push(index);
            lines.push(line);
            used += separator + length;
        }
        Ok((batch, lines.join("\n")))
    }

    /// `_send`: `true` unless the notifier reported a failure or raised.
    async fn send<P: SitePorts>(
        &self,
        ports: &P,
        error_type: &'static str,
        error_msg: String,
        status: &'static str,
        opsgate_ticket: bool,
    ) -> bool {
        let message = SiteMessage {
            incident_key: SITE_INCIDENT_KEY,
            name: SITE_INCIDENT_NAME,
            error_type,
            error_msg,
            status,
            opsgate_ticket,
        };
        match ports.notify(&message).await {
            Ok(delivery) => delivery != SiteDelivery::Failed,
            Err(error) => {
                ports.log(
                    LogLevel::Error,
                    &format!("failed to send the site connectivity {error_type}"),
                    Some(&error),
                );
                false
            }
        }
    }

    // ------------------------------------------------------------ recheck

    /// `_process_recheck`: in `enforce`, reschedule held dependents whose
    /// whole dependency is usable. The in-flight count is read before the
    /// held set.
    async fn process_recheck<P: SitePorts>(&mut self, ports: &P, now: i64) -> Result<(), Raised> {
        if self.config.mode != SiteMode::Enforce || !truthy(self.recheck.get("pending")) {
            return Ok(());
        }
        let release_at = as_int(self.recheck.get("release_at"))?;
        let queried = match ports.count_processing_claims_before(release_at).await {
            Ok(in_flight) => match ports.list_held_checks().await {
                Ok(held) => Ok((in_flight, held)),
                Err(error) => Err(error),
            },
            Err(error) => Err(error),
        };
        let (in_flight, held) = match queried {
            Ok(result) => result,
            Err(error) => {
                ports.log(
                    LogLevel::Error,
                    "site connectivity recheck query failed",
                    Some(&error),
                );
                return Ok(());
            }
        };

        let usable: Vec<&HeldCheck> = held
            .iter()
            .filter(|row| {
                resolve_site_dependency(&self.warnings, row.check_id, &row.data)
                    .is_some_and(|dependency| self.snapshot.dependency_usable(Some(&dependency)))
            })
            .collect();
        let due: Vec<i64> = usable
            .iter()
            .filter(|row| row.status == "idle" && !row.disabled && row.next_check_time > now)
            .map(|row| row.check_id)
            .collect();
        if !due.is_empty()
            && let Err(error) = ports.reschedule_checks(&due, now).await
        {
            let ids: Vec<String> = due.iter().map(i64::to_string).collect();
            ports.log(
                LogLevel::Error,
                &format!("failed to reschedule held checks [{}]", ids.join(", ")),
                Some(&error),
            );
            return Ok(());
        }
        let pending = in_flight > 0 || !usable.is_empty();
        if pending == truthy(self.recheck.get("pending")) {
            return Ok(());
        }
        let mut recheck = PyDict::new();
        recheck.insert("pending".to_owned(), PyJson::Bool(pending));
        recheck.insert("release_at".to_owned(), PyJson::Int(release_at));
        self.recheck = recheck;
        ports.log(
            LogLevel::Info,
            &format!(
                "site connectivity recheck for release {} completed",
                iso(release_at)?
            ),
            None,
        );
        self.write_payload(ports, now).await;
        Ok(())
    }

    // -------------------------------------------------------- persistence

    fn is_fresh(&self, now: i64) -> bool {
        i128::from(now) - i128::from(self.observed_at)
            <= i128::from(self.config.stale_after_seconds())
    }

    fn build_snapshot(&self) -> SiteSnapshot {
        SiteSnapshot {
            paths: self.paths.each_ref().map(|path| {
                Some(PathSnapshot {
                    state: path.state,
                    down_since: path.down_since,
                    recovered_at: path.recovered_at,
                    release_at: path.release_at,
                    last_release_at: path.last_release_at,
                })
            }),
            observed_at: self.observed_at,
            mode: self.config.mode,
            incident_id: self.incident_id,
        }
    }

    /// `_publish_snapshot`.
    fn publish<P: SitePorts>(&mut self, ports: &P) {
        self.snapshot = self.build_snapshot();
        ports.publish(&self.snapshot);
    }

    /// `build_payload`: the whole persisted lifecycle, keys in Python order.
    pub fn build_payload(&self) -> PyDict {
        let mut paths = PyDict::new();
        for path in self.observed_paths() {
            let mut entry = PyDict::new();
            entry.insert("state".to_owned(), PyJson::from(path.state.as_str()));
            entry.insert("down_since".to_owned(), PyJson::Int(path.down_since));
            entry.insert("recovered_at".to_owned(), PyJson::Int(path.recovered_at));
            entry.insert("release_at".to_owned(), PyJson::Int(path.release_at));
            entry.insert(
                "last_release_at".to_owned(),
                PyJson::Int(path.last_release_at),
            );
            paths.insert(path.name.as_str().to_owned(), PyJson::Dict(entry));
        }
        let mut payload = PyDict::new();
        payload.insert("incident_type".to_owned(), PyJson::from(SITE_INCIDENT_TYPE));
        payload.insert("version".to_owned(), PyJson::Int(SITE_PAYLOAD_VERSION));
        payload.insert("phase".to_owned(), PyJson::from(self.phase.as_str()));
        payload.insert("incident_id".to_owned(), PyJson::Int(self.incident_id));
        payload.insert("observed_at".to_owned(), PyJson::Int(self.observed_at));
        payload.insert("paths".to_owned(), PyJson::Dict(paths));
        payload.insert("ongoing".to_owned(), PyJson::Dict(self.ongoing.clone()));
        payload.insert(
            "summaries".to_owned(),
            PyJson::List(self.summaries.iter().cloned().map(PyJson::Dict).collect()),
        );
        payload.insert("recheck".to_owned(), PyJson::Dict(self.recheck.clone()));
        payload
    }

    /// `_write_payload`: replace the row's payload, create the row when it
    /// is missing; any error marks the row unknown and returns `false`.
    async fn write_payload<P: SitePorts>(&mut self, ports: &P, now: i64) -> bool {
        let payload = self.build_payload();
        if self.row_present {
            match ports.set_incident_payload(&payload).await {
                Ok(true) => return true,
                Ok(false) => self.row_present = false,
                Err(error) => {
                    self.persist_failed(ports, &error);
                    return false;
                }
            }
        }
        match ports.open_incident(now, &payload).await {
            Ok(()) => {
                self.row_present = true;
                true
            }
            Err(error) => {
                self.persist_failed(ports, &error);
                false
            }
        }
    }

    fn persist_failed<P: SitePorts>(&mut self, ports: &P, error: &PortError) {
        ports.log(
            LogLevel::Error,
            "failed to persist the site connectivity incident",
            Some(error),
        );
        self.row_present = false;
    }
}
