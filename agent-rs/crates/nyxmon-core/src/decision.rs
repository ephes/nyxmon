//! The notification decision (`service_layer/handlers.py:_should_notify_check_result`),
//! PLAN.md section 5.3.
//!
//! [`decide`] is a pure function of the notification state that was read,
//! the sample, the site snapshot and the knobs. It returns the
//! compare-and-swap transition and annotates the sample's data with the
//! `site_connectivity` reason exactly like Python. The branch order is part
//! of the contract.

use num_bigint::BigInt;

use crate::env::{Env, SiteEnvConfig, SiteMode};
use crate::log::WarnOnce;
use crate::model::{Check, NotificationDecision, NotificationState, ResultStatus};
use crate::policy::resolve_notification_policy;
use crate::pyjson::{PyDict, PyJson, py_truthy};
use crate::site::{HoldReason, SiteSnapshot, resolve_site_dependency};

/// `SITE_CONNECTIVITY_DATA_KEY`: why a sample was (or would have been) held.
pub const SITE_CONNECTIVITY_DATA_KEY: &str = "site_connectivity";

/// The hold bounds of the observer's configuration (`_site_config`); the
/// defaults when the feature is off.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SiteLimits {
    pub stale_after: i64,
    pub max_hold: i64,
}

impl SiteLimits {
    pub fn from_config(config: &SiteEnvConfig) -> Self {
        SiteLimits {
            stale_after: config.stale_after_seconds(),
            max_hold: config.max_hold_seconds,
        }
    }
}

impl Default for SiteLimits {
    /// `_DEFAULT_SITE_CONFIG`.
    fn default() -> Self {
        SiteLimits::from_config(&SiteEnvConfig::default())
    }
}

/// Everything [`decide`] reads besides the sample and its state.
#[derive(Clone, Copy, Debug)]
pub struct DecisionContext<'a> {
    /// Global policy defaults.
    pub env: &'a Env,
    /// Warn-once state of the policy and dependency readers.
    pub warnings: &'a WarnOnce,
    /// The latest snapshot; `None` when the feature is off (`NullSiteState`).
    pub snapshot: Option<&'a SiteSnapshot>,
    pub limits: SiteLimits,
    /// `delivery_retry_seconds()`, 0 when the retry is off.
    pub retry_seconds: i64,
    /// `_notify_immediate_cooldown_seconds()` (unbounded positive int).
    pub immediate_cooldown: &'a BigInt,
}

/// `_with_attempt`: the durable intent to notify, recorded before any I/O.
fn with_attempt(state: NotificationState, now: i64) -> NotificationState {
    NotificationState {
        attempt_seq: state.attempt_seq.saturating_add(1),
        attempt_at: now,
        ..state
    }
}

/// `_annotate_site_connectivity`: `{**data, "site_connectivity": {"held":
/// held, **reason}}` (an existing key keeps its position).
fn annotate(data: &mut PyDict, held: bool, reason: &HoldReason, exhausted: bool) {
    let mut value = PyDict::new();
    value.insert("held".to_owned(), PyJson::Bool(held));
    value.extend(reason.to_dict());
    if exhausted {
        value.insert("exhausted".to_owned(), PyJson::Bool(true));
    }
    data.insert(SITE_CONNECTIVITY_DATA_KEY.to_owned(), PyJson::Dict(value));
}

fn truthy_key(data: &PyDict, key: &str) -> bool {
    data.get(key).is_some_and(py_truthy)
}

/// `a - b >= limit` on Python integers.
fn elapsed_at_least(now: i64, since: i64, limit: i128) -> bool {
    i128::from(now) - i128::from(since) >= limit
}

/// The decision for one sample.
///
/// * `state` is the notification state read for the check;
/// * `data` is the sample's result data; it is annotated in place;
/// * `force_notification` is never set by a production path (PLAN.md 1.2);
/// * `now` is `int(time.time())`, taken by the caller. Python reads the
///   clock only after the suppression branch, so the early branches report
///   `now = 0` like the dataclass default.
///
/// Python's second branch ("status is neither error nor warning") cannot
/// occur with [`ResultStatus`]; the OK branch covers the only other value.
pub fn decide(
    ctx: &DecisionContext<'_>,
    state: NotificationState,
    check: &Check,
    status: ResultStatus,
    data: &mut PyDict,
    force_notification: bool,
    now: i64,
) -> NotificationDecision {
    let unchanged = |next_state: NotificationState, should_notify: bool| NotificationDecision {
        should_notify,
        expected: state,
        next_state,
        held: false,
        attempt_seq: 0,
        now: 0,
    };
    if status == ResultStatus::Ok {
        // Recovery closes the incident; `attempt_seq` is a monotonic fence.
        return unchanged(state.cleared(), false);
    }
    if force_notification {
        return unchanged(state, true);
    }
    if truthy_key(data, "collector_internal") {
        // An expired lease is recorded but inert for alerting.
        return unchanged(state, false);
    }
    if truthy_key(data, "notification_suppressed") {
        // Maintenance breaks the incident but keeps the hold and the
        // pending delivery intent.
        return unchanged(state.with_streak_reset(), false);
    }

    let limits = ctx.limits;
    // Always resolved, so a malformed declaration warns even when the
    // feature is off.
    let dependency = resolve_site_dependency(ctx.warnings, check.check_id, &check.data);
    let claim = check.claim_started_at;
    let mut hold = None;
    let mut recovered = false;
    if let (Some(dependency), Some(snapshot)) = (dependency.as_ref(), ctx.snapshot) {
        hold = snapshot.hold_reason(Some(dependency), claim, now, limits.stale_after);
        recovered = snapshot.dependency_recovered(Some(dependency), claim, now, limits.stale_after);
        if hold.is_none()
            && snapshot.mode == SiteMode::Observe
            && let Some(observed) =
                snapshot.observed_reason(Some(dependency), claim, now, limits.stale_after)
        {
            // Observe mode records the judgement without acting on it.
            annotate(data, false, &observed, false);
        }
    }

    // A hold is bounded by `max_hold`; the budget re-arms only on an
    // observed recovery.
    let exhausted = state.held_since > 0
        && elapsed_at_least(now, state.held_since, i128::from(limits.max_hold));
    if let Some(reason) = &hold {
        if !exhausted {
            annotate(data, true, reason, false);
            return NotificationDecision {
                should_notify: false,
                expected: state,
                next_state: NotificationState {
                    failure_count: state.failure_count.saturating_add(1),
                    first_failure_at: or_now(state.first_failure_at, now),
                    held_since: or_now(state.held_since, now),
                    ..state
                },
                held: true,
                attempt_seq: 0,
                now,
            };
        }
        annotate(data, false, reason, true);
    }

    let held_since = if recovered { 0 } else { state.held_since };

    // A pending, unacknowledged attempt is repeated whatever branch claimed
    // it; held samples were decided above and are never repeated.
    let retry_due = ctx.retry_seconds > 0
        && state.attempt_at > 0
        && elapsed_at_least(now, state.attempt_at, i128::from(ctx.retry_seconds));

    if truthy_key(data, "notification_immediate") {
        // The cooldown bounds new alerts, not the redelivery of one.
        let cooldown = i128::try_from(ctx.immediate_cooldown).unwrap_or(i128::MAX);
        let should_notify = retry_due || elapsed_at_least(now, state.last_immediate_at, cooldown);
        let mut next_state = NotificationState {
            last_immediate_at: if should_notify {
                now
            } else {
                state.last_immediate_at
            },
            held_since,
            ..state
        };
        if should_notify {
            next_state = with_attempt(next_state, now);
        }
        return finish(state, next_state, should_notify, now);
    }

    let policy =
        resolve_notification_policy(ctx.env, ctx.warnings, check.check_id, &check.data, status);
    let failure_count = state.failure_count.saturating_add(1);
    let should_notify = if retry_due {
        true
    } else if state.last_notified_at == 0 {
        // No alert yet for this incident: the initial-alert threshold.
        failure_count >= policy.consecutive_failures
    } else {
        // Open incident: remind on elapsed wall-clock time.
        elapsed_at_least(
            now,
            state.last_notified_at,
            i128::from(policy.reminder_seconds),
        )
    };
    // Derived from the state that was read, so the compare-and-swap cannot
    // silently reset a field this function forgot.
    let mut next_state = NotificationState {
        failure_count,
        last_attempt_count: if should_notify {
            failure_count
        } else {
            state.last_attempt_count
        },
        last_notified_at: if should_notify {
            now
        } else {
            state.last_notified_at
        },
        first_failure_at: or_now(state.first_failure_at, now),
        held_since,
        ..state
    };
    if should_notify {
        next_state = with_attempt(next_state, now);
    }
    finish(state, next_state, should_notify, now)
}

/// `value or now`.
fn or_now(value: i64, now: i64) -> i64 {
    if value == 0 { now } else { value }
}

fn finish(
    expected: NotificationState,
    next_state: NotificationState,
    should_notify: bool,
    now: i64,
) -> NotificationDecision {
    NotificationDecision {
        should_notify,
        expected,
        next_state,
        held: false,
        attempt_seq: if should_notify {
            next_state.attempt_seq
        } else {
            0
        },
        now,
    }
}
