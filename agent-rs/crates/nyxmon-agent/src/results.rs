//! Result handling (`service_layer/handlers.py:add_check_result` and the
//! `result_received` callback of `execute_checks`; PLAN.md 3.4, 5.3).
//!
//! Per sample: maintenance suppression (fetched once, only for error and
//! warning samples that are neither forced nor collector-internal), then up
//! to [`MAX_NOTIFICATION_STATE_ATTEMPTS`] rounds of read state, decide,
//! persist with the compare-and-swap transition, then the notification and
//! its acknowledgement. Any error is logged as `result handling failed for
//! check_id=<id>` and never stops the sibling results.

use std::fmt;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use nyxmon_checks::suppression_fetch::fetch_payload;
use nyxmon_core::decision::{DecisionContext, SiteLimits, decide};
use nyxmon_core::env::{Env, delivery_retry_seconds, notify_immediate_cooldown_seconds};
use nyxmon_core::log::WarnOnce;
use nyxmon_core::model::{Check, CheckResult, NotificationState, ResultStatus};
use nyxmon_core::pyjson::{PyJson, PyNumError, py_truthy};
use nyxmon_core::site::SiteSnapshot;
use nyxmon_core::suppression::{SuppressionRequest, evaluate, suppression_request};
use nyxmon_notify::{Notifier, NotifyRaised};
use nyxmon_store::{PersistRequest, Store, StoreError};

/// `MAX_NOTIFICATION_STATE_ATTEMPTS`.
pub const MAX_NOTIFICATION_STATE_ATTEMPTS: usize = 3;

/// The store operations of the result path.
pub trait ResultStore: Send + Sync + 'static {
    fn get_notification_state(
        &self,
        check_id: i64,
    ) -> impl Future<Output = Result<NotificationState, StoreError>> + Send;

    fn persist_check_result(
        &self,
        request: PersistRequest,
    ) -> impl Future<Output = Result<bool, StoreError>> + Send;

    fn acknowledge_notification_attempt(
        &self,
        check_id: i64,
        attempt_seq: i64,
    ) -> impl Future<Output = Result<bool, StoreError>> + Send;
}

impl ResultStore for Store {
    fn get_notification_state(
        &self,
        check_id: i64,
    ) -> impl Future<Output = Result<NotificationState, StoreError>> + Send {
        Store::get_notification_state(self, check_id)
    }

    fn persist_check_result(
        &self,
        request: PersistRequest,
    ) -> impl Future<Output = Result<bool, StoreError>> + Send {
        Store::persist_check_result(self, request)
    }

    fn acknowledge_notification_attempt(
        &self,
        check_id: i64,
        attempt_seq: i64,
    ) -> impl Future<Output = Result<bool, StoreError>> + Send {
        Store::acknowledge_notification_attempt(self, check_id, attempt_seq)
    }
}

/// `int(time.time())`.
pub type Clock = Arc<dyn Fn() -> i64 + Send + Sync>;

/// The wall clock in whole seconds.
pub fn system_clock() -> Clock {
    Arc::new(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| {
                i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
            })
    })
}

/// The maintenance-suppression fetch: the decoded payload, or `None` on any
/// failure (fail open).
pub type SuppressionFetch = Arc<
    dyn Fn(SuppressionRequest) -> Pin<Box<dyn Future<Output = Option<PyJson>> + Send>>
        + Send
        + Sync,
>;

/// `_fetch_payload` over HTTP, one fresh session per fetch.
pub fn http_suppression_fetch() -> SuppressionFetch {
    Arc::new(|request| Box::pin(async move { fetch_payload(&request).await }))
}

/// Where the result handler reads the site connectivity state from
/// (`SiteStateProvider`).
pub trait SiteState: Send + Sync {
    /// The latest snapshot; `None` when nothing is observed.
    fn snapshot(&self) -> Option<SiteSnapshot>;
    /// The hold bounds (`_site_config`).
    fn limits(&self) -> SiteLimits;
}

/// `NullSiteState`: site connectivity off, nothing is ever held.
#[derive(Clone, Copy, Debug, Default)]
pub struct NullSiteState;

impl SiteState for NullSiteState {
    fn snapshot(&self) -> Option<SiteSnapshot> {
        None
    }

    fn limits(&self) -> SiteLimits {
        SiteLimits::default()
    }
}

/// An exception escaping `add_check_result`.
#[derive(Debug)]
pub enum HandlerError {
    Store(StoreError),
    /// A Python number conversion that raises in the suppression path.
    Value(PyNumError),
    /// The notifier raised (PLAN.md 4.2 `NotifyRaised`).
    Notify(NotifyRaised),
}

impl fmt::Display for HandlerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            HandlerError::Store(err) => write!(f, "{err}"),
            HandlerError::Value(err) => write!(f, "{}: {err}", err.python_class()),
            HandlerError::Notify(err) => write!(f, "{err}"),
        }
    }
}

impl std::error::Error for HandlerError {}

impl From<StoreError> for HandlerError {
    fn from(err: StoreError) -> Self {
        HandlerError::Store(err)
    }
}

impl From<PyNumError> for HandlerError {
    fn from(err: PyNumError) -> Self {
        HandlerError::Value(err)
    }
}

impl From<NotifyRaised> for HandlerError {
    fn from(err: NotifyRaised) -> Self {
        HandlerError::Notify(err)
    }
}

/// The result handler of one agent process.
pub struct ResultHandler<S> {
    store: S,
    notifier: Arc<dyn Notifier>,
    env: Arc<Env>,
    warnings: Arc<WarnOnce>,
    site: Arc<dyn SiteState>,
    clock: Clock,
    fetch: SuppressionFetch,
}

impl<S: ResultStore> ResultHandler<S> {
    /// A handler on the wall clock, without site connectivity, fetching
    /// suppression payloads over HTTP.
    pub fn new(
        store: S,
        notifier: Arc<dyn Notifier>,
        env: Arc<Env>,
        warnings: Arc<WarnOnce>,
    ) -> Self {
        ResultHandler {
            store,
            notifier,
            env,
            warnings,
            site: Arc::new(NullSiteState),
            clock: system_clock(),
            fetch: http_suppression_fetch(),
        }
    }

    pub fn with_clock(mut self, clock: Clock) -> Self {
        self.clock = clock;
        self
    }

    pub fn with_site_state(mut self, site: Arc<dyn SiteState>) -> Self {
        self.site = site;
        self
    }

    pub fn with_suppression_fetch(mut self, fetch: SuppressionFetch) -> Self {
        self.fetch = fetch;
        self
    }

    pub fn store(&self) -> &S {
        &self.store
    }

    /// The process notifier (also used for collector incidents).
    pub fn notifier(&self) -> &Arc<dyn Notifier> {
        &self.notifier
    }

    /// The process-wide warn-once state.
    pub fn warnings(&self) -> &Arc<WarnOnce> {
        &self.warnings
    }

    /// `int(time.time())` of this handler.
    pub fn now(&self) -> i64 {
        (self.clock)()
    }

    /// `execute_checks.result_received`: schedule the next run of `check`
    /// and record `result`; a failure is logged and swallowed.
    pub async fn receive(&self, mut check: Check, result: CheckResult) {
        check.schedule_next_check(self.now());
        let check_id = result.check_id;
        if let Err(err) = self.add_check_result(&check, result, false).await {
            tracing::error!(
                target: "nyxmon.service_layer.handlers",
                "result handling failed for check_id={check_id}\n{err}"
            );
        }
    }

    /// `notification_suppression_details(check)`: fetched only when
    /// configured.
    async fn suppression_details(
        &self,
        check: &Check,
    ) -> Result<Option<nyxmon_core::pyjson::PyDict>, PyNumError> {
        let Some(request) = suppression_request(&check.data)? else {
            return Ok(None);
        };
        let now = self.now();
        let url = request.url.clone();
        let Some(payload) = (self.fetch)(request).await else {
            return Ok(None);
        };
        let Some(config) = check
            .data
            .get("notification_suppression")
            .and_then(PyJson::as_dict)
        else {
            return Ok(None);
        };
        evaluate(config, &url, &payload, now)
    }

    /// `add_check_result`: persist the sample and alert about it when policy
    /// and site state allow. Returns whether a notification was triggered.
    ///
    /// # Errors
    /// Store failures (other than an exhausted notification-state conflict),
    /// exceptions of the suppression configuration and a raising notifier;
    /// a failed acknowledgement is only logged.
    pub async fn add_check_result(
        &self,
        check: &Check,
        mut result: CheckResult,
        force_notification: bool,
    ) -> Result<bool, HandlerError> {
        if matches!(result.status, ResultStatus::Error | ResultStatus::Warning)
            && !force_notification
            && !result.data.get("collector_internal").is_some_and(py_truthy)
            && let Some(details) = self.suppression_details(check).await?
            && !details.is_empty()
        {
            result
                .data
                .insert("notification_suppressed".to_owned(), PyJson::Dict(details));
        }
        let retry_seconds = delivery_retry_seconds(&self.env);
        let mut should_notify = false;
        let mut attempt_seq = 0;
        for attempt in 0..MAX_NOTIFICATION_STATE_ATTEMPTS {
            let cooldown = notify_immediate_cooldown_seconds(&self.env);
            let state = self.store.get_notification_state(check.check_id).await?;
            let snapshot = self.site.snapshot();
            let ctx = DecisionContext {
                env: &self.env,
                warnings: &self.warnings,
                snapshot: snapshot.as_ref(),
                limits: self.site.limits(),
                retry_seconds,
                immediate_cooldown: &cooldown,
            };
            let now = self.now();
            let decision = decide(
                &ctx,
                state,
                check,
                result.status,
                &mut result.data,
                force_notification,
                now,
            );
            let request = PersistRequest {
                check: check.clone(),
                result: result.clone(),
                transition: Some((decision.expected, decision.next_state)),
                complete_check: !force_notification,
                hold_marker: None,
            };
            match self.store.persist_check_result(request).await {
                Ok(persisted) => {
                    should_notify = persisted && decision.should_notify;
                    attempt_seq = if persisted { decision.attempt_seq } else { 0 };
                    break;
                }
                Err(StoreError::NotificationStateConflict { .. })
                    if attempt == MAX_NOTIFICATION_STATE_ATTEMPTS - 1 =>
                {
                    tracing::error!(
                        target: "nyxmon.service_layer.handlers",
                        "notification state remained contended for check_id={}; persisting \
                         result without changing alert state",
                        check.check_id
                    );
                    // The streak bookkeeping is dropped, but a hold is not.
                    let fallback = PersistRequest {
                        check: check.clone(),
                        result: result.clone(),
                        transition: None,
                        complete_check: !force_notification,
                        hold_marker: decision.held.then_some(decision.now),
                    };
                    self.store.persist_check_result(fallback).await?;
                    should_notify = false;
                    attempt_seq = 0;
                    break;
                }
                Err(StoreError::NotificationStateConflict { .. }) => {
                    tracing::warn!(
                        target: "nyxmon.service_layer.handlers",
                        "notification state changed concurrently for check_id={}; retrying",
                        check.check_id
                    );
                }
                Err(err) => return Err(err.into()),
            }
        }

        if should_notify {
            let delivery = self.notifier.notify_check_failed(check, &result).await?;
            // Acknowledged unless the send failed, whatever the retry knob.
            if !delivery.is_failure()
                && attempt_seq > 0
                && let Err(err) = self
                    .store
                    .acknowledge_notification_attempt(check.check_id, attempt_seq)
                    .await
            {
                tracing::error!(
                    target: "nyxmon.service_layer.handlers",
                    "failed to acknowledge notification attempt {attempt_seq} for \
                     check_id={}\n{err}",
                    check.check_id
                );
            }
        }
        Ok(should_notify)
    }
}

#[cfg(test)]
mod tests;
