//! The `json-metrics` executor (`executors/json_metrics_executor.py`).
//!
//! The evaluation is pure ([`nyxmon_core::metrics_eval`]); this module does
//! the fetch on the check's own session copy (its own cookie jar):
//! redirects followed, basic auth, the body of an error response not read,
//! the body cap applied to the decoded bytes, and `json.loads` on the raw
//! bytes. Each attempt, redirects and body read included, is bounded by the
//! configured timeout as a whole ([`attempt_deadline`]).
//!
//! Retries stay inside the check's execution budget: the runtime estimate
//! the collector sizes the processing lease with
//! ([`estimated_check_runtime_seconds`], capped at
//! [`MAX_DERIVED_PROCESSING_LEASE_SECONDS`]). A retry starts only while its
//! delay plus a whole attempt still fit into that budget; otherwise the
//! last failure is the result.

use std::time::{Duration, Instant};

use hyper::header::HeaderValue;
use nyxmon_core::config::json_metrics::JsonMetricsConfig;
use nyxmon_core::config::{ConfigError, PyException};
use nyxmon_core::lease::{MAX_DERIVED_PROCESSING_LEASE_SECONDS, estimated_check_runtime_seconds};
use nyxmon_core::metrics_eval::{evaluate, failure_status, failure_summary};
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson, loads_bytes, py_type_name as type_name};

use crate::ctx::BatchCtx;
use crate::http_client::{
    BodyError, BodyLimit, GetRequest, HttpError, HttpErrorKind, HttpSession, attempt_deadline,
    basic_authorization, read_body, timeout_duration,
};

fn error(check: &Check, error_type: &str, msg: impl Into<String>) -> CheckResult {
    let mut data = PyDict::new();
    data.insert("error_type".to_owned(), PyJson::from(error_type));
    data.insert("error_msg".to_owned(), PyJson::from(msg.into()));
    CheckResult::new(check.check_id, ResultStatus::Error, data)
}

/// `_build_auth(config)`: the `Authorization` value, or the exception httpx
/// raises for an unusable `auth` (reported as `unexpected_error`).
fn authorization(auth: Option<&PyJson>) -> Result<Option<HeaderValue>, String> {
    let Some(auth) = auth else {
        return Ok(None);
    };
    let (username, password) = match auth {
        PyJson::Dict(map) => (&map["username"], &map["password"]),
        PyJson::List(_) => {
            return Err("list indices must be integers or slices, not str".to_owned());
        }
        PyJson::Str(_) => return Err("string indices must be integers, not 'str'".to_owned()),
        other => {
            return Err(format!(
                "'{}' object is not subscriptable",
                type_name(other)
            ));
        }
    };
    match (username, password) {
        (PyJson::Str(user), PyJson::Str(pass)) => Ok(Some(basic_authorization(user, pass))),
        (PyJson::Str(_), other) => Err(format!(
            "sequence item 1: expected a bytes-like object, {} found",
            type_name(other)
        )),
        (other, _) => Err(format!(
            "sequence item 0: expected a bytes-like object, {} found",
            type_name(other)
        )),
    }
}

enum Fetched {
    Body(u16, Vec<u8>),
    TooLarge(String),
    Failed(HttpError),
}

async fn fetch(
    http: &HttpSession,
    config: &JsonMetricsConfig,
    url: &str,
    auth: Option<HeaderValue>,
) -> Fetched {
    let request = GetRequest {
        url,
        timeout: timeout_duration(config.timeout),
        follow_redirects: true,
        authorization: auth,
    };
    let attempt = async {
        let response = http.get(&request).await?;
        let status = response.status;
        if status >= 400 {
            return Ok(Fetched::Body(status, Vec::new()));
        }
        let limit = BodyLimit {
            limit: &config.max_body_bytes,
        };
        match read_body(response, Some(limit)).await {
            Ok(body) => Ok(Fetched::Body(status, body)),
            Err(BodyError::TooLarge(message)) => Ok(Fetched::TooLarge(message)),
            Err(BodyError::Http(err)) => Err(err),
        }
    };
    attempt_deadline(request.timeout, attempt)
        .await
        .unwrap_or_else(Fetched::Failed)
}

/// `JsonMetricsExecutor.execute`.
///
/// # Errors
/// The exception Python's executor lets escape (a malformed config, or a
/// non-string check `path` reaching the evaluation); the runner reports it
/// as `executor_exception`.
pub async fn execute(check: &Check, ctx: &BatchCtx) -> Result<CheckResult, PyException> {
    let config = match JsonMetricsConfig::from_data(&check.data) {
        Ok(config) => config,
        Err(ConfigError::Invalid(message)) => {
            return Ok(error(check, "configuration_error", message));
        }
        Err(ConfigError::Exception(exc)) => return Err(exc),
    };
    let attempts = config.retries + 1;
    let started = Instant::now();
    let budget = execution_budget(check);
    // `attempt < retries`, and the delay plus one more whole attempt still
    // fit into the execution budget.
    let may_retry = |attempt: u32| {
        attempt < config.retries
            && started.elapsed().as_secs_f64() + config.retry_delay + config.timeout
                <= budget.as_secs_f64()
    };
    let mut last_error = None;
    let http = ctx.http.for_check();

    for attempt in 0..attempts {
        let start = Instant::now();
        // Arguments of `client.stream(...)`: the auth first, then the URL.
        let auth = match authorization(config.auth.as_ref()) {
            Ok(auth) => auth,
            Err(message) => return Ok(error(check, "unexpected_error", message)),
        };
        let PyJson::Str(url) = &config.url else {
            return Ok(error(
                check,
                "unexpected_error",
                format!(
                    "Invalid type for url.  Expected str or httpx.URL, got <class '{}'>",
                    type_name(&config.url)
                ),
            ));
        };
        let (status, raw_body) = match fetch(&http, &config, url, auth).await {
            Fetched::Body(status, body) => (status, body),
            Fetched::TooLarge(message) => return Ok(error(check, "body_too_large", message)),
            Fetched::Failed(err) => {
                let error_type = match err.kind {
                    HttpErrorKind::Timeout => "timeout",
                    HttpErrorKind::Connect | HttpErrorKind::Request => "request_error",
                    HttpErrorKind::Unexpected => {
                        return Ok(error(check, "unexpected_error", err.message));
                    }
                };
                let failure = error(check, error_type, err.message);
                if may_retry(attempt) {
                    last_error = Some(failure);
                    ctx.sleeper.sleep(config.retry_delay).await;
                    continue;
                }
                return Ok(failure);
            }
        };
        if status >= 400 {
            let failure = error(check, "http_error", format!("HTTP {status}"));
            if (500..600).contains(&status) && may_retry(attempt) {
                last_error = Some(failure);
                ctx.sleeper.sleep(config.retry_delay).await;
                continue;
            }
            return Ok(failure);
        }
        let body = match loads_bytes(&raw_body) {
            Ok(body) => body,
            Err(err) if err.is_value_error() => {
                return Ok(error(check, "json_error", err.to_string()));
            }
            Err(err) => return Ok(error(check, "unexpected_error", err.to_string())),
        };

        let duration_ms = i64::try_from(start.elapsed().as_millis()).unwrap_or(i64::MAX);
        let failures = evaluate(&body, &config.checks)?;
        let mut data = PyDict::new();
        if failures.is_empty() {
            data.insert("duration_ms".to_owned(), PyJson::Int(duration_ms));
            return Ok(CheckResult::new(check.check_id, ResultStatus::Ok, data));
        }
        let status = failure_status(&failures);
        data.insert("error_type".to_owned(), PyJson::from("threshold_failed"));
        data.insert(
            "error_msg".to_owned(),
            PyJson::from(failure_summary(&failures)),
        );
        data.insert("failures".to_owned(), PyJson::List(failures));
        data.insert("duration_ms".to_owned(), PyJson::Int(duration_ms));
        return Ok(CheckResult::new(check.check_id, status, data));
    }
    Ok(last_error.unwrap_or_else(|| error(check, "unexpected_error", "metrics check failed")))
}

/// The wall-clock budget of one execution: the check's runtime estimate
/// (an explicit `max_runtime_seconds`, else retries, delays and timeouts
/// plus a margin), never above the largest derived processing lease.
fn execution_budget(check: &Check) -> Duration {
    let estimate =
        estimated_check_runtime_seconds(check.check_id, &check.check_type, &check.data, None)
            .clamp(0, MAX_DERIVED_PROCESSING_LEASE_SECONDS);
    Duration::from_secs(u64::try_from(estimate).unwrap_or(0))
}

#[cfg(test)]
mod tests;
