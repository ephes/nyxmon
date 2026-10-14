//! The `http` / `json-http` executor (`executors/http_executor.py`).
//!
//! The check fetches the status line and headers only: the final response
//! body is never read (`_fetch_headers` streams and closes), redirect bodies
//! are (httpx reads them before following).

use std::time::Instant;

use hyper::header;
use nyxmon_core::config::http::HttpConfig;
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson};

use crate::ctx::BatchCtx;
use crate::http_client::{GetRequest, HttpErrorKind, header_text, timeout_duration};

fn result(check: &Check, status: ResultStatus, entries: Vec<(&str, PyJson)>) -> CheckResult {
    let data: PyDict = entries
        .into_iter()
        .map(|(key, value)| (key.to_owned(), value))
        .collect();
    CheckResult::new(check.check_id, status, data)
}

fn error(check: &Check, error_type: &str, msg: String, rest: Vec<(&str, PyJson)>) -> CheckResult {
    let mut entries = vec![
        ("error_type", PyJson::from(error_type)),
        ("error_msg", PyJson::from(msg)),
    ];
    entries.extend(rest);
    result(check, ResultStatus::Error, entries)
}

fn attempt_keys(attempt: u32, attempts: u32) -> Vec<(&'static str, PyJson)> {
    vec![
        ("attempt", PyJson::Int(i64::from(attempt))),
        ("attempts", PyJson::Int(i64::from(attempts))),
    ]
}

/// `HttpCheckExecutor.execute`.
pub async fn execute(check: &Check, ctx: &BatchCtx) -> CheckResult {
    let config = match HttpConfig::from_data(&check.data) {
        Ok(config) => config,
        Err(message) => {
            return error(check, "configuration_error", message, attempt_keys(0, 0));
        }
    };
    let attempts = config.retries + 1;
    let request = GetRequest {
        url: &check.url,
        timeout: timeout_duration(config.timeout),
        follow_redirects: config.follow_redirects,
        authorization: None,
        timeout_overflows: false,
    };
    for attempt in 1..=attempts {
        let start = Instant::now();
        let response = match ctx.http.get(&request).await {
            Ok(response) => response,
            Err(err) => {
                let error_type = match err.kind {
                    HttpErrorKind::Timeout => "timeout",
                    HttpErrorKind::Connect => "connection_error",
                    HttpErrorKind::Request => "request_error",
                    HttpErrorKind::Unexpected => {
                        return error(
                            check,
                            "unexpected_error",
                            err.message,
                            attempt_keys(attempt, attempts),
                        );
                    }
                };
                if attempt < attempts {
                    ctx.sleeper.sleep(config.retry_delay).await;
                    continue;
                }
                return error(
                    check,
                    error_type,
                    err.message,
                    attempt_keys(attempt, attempts),
                );
            }
        };
        let status_code = i64::from(response.status);
        let location = header_text(&response.headers, header::LOCATION);
        // The body is never read: dropping the response closes the stream.
        drop(response);
        let retryable = config.retry_status_codes.contains(&status_code) && attempt < attempts;

        if let Some(expected) = config.expected_status {
            if status_code != expected {
                if retryable {
                    ctx.sleeper.sleep(config.retry_delay).await;
                    continue;
                }
                let mut rest = vec![
                    ("expected_status", PyJson::Int(expected)),
                    ("status_code", PyJson::Int(status_code)),
                ];
                rest.extend(attempt_keys(attempt, attempts));
                return error(
                    check,
                    "unexpected_status",
                    format!("Expected HTTP {expected}, got {status_code}"),
                    rest,
                );
            }
        } else if !(200..300).contains(&status_code) {
            if retryable {
                ctx.sleeper.sleep(config.retry_delay).await;
                continue;
            }
            let mut rest = vec![("status_code", PyJson::Int(status_code))];
            rest.extend(attempt_keys(attempt, attempts));
            return error(check, "http_error", format!("HTTP {status_code}"), rest);
        }

        if let Some(expected) = &config.expected_location
            && location.as_deref() != Some(expected.as_str())
        {
            let mut rest = vec![
                ("expected_location", PyJson::from(expected.as_str())),
                ("location", location.map_or(PyJson::Null, PyJson::from)),
            ];
            rest.extend(attempt_keys(attempt, attempts));
            return error(
                check,
                "unexpected_location",
                "HTTP Location header does not match the configured target".to_owned(),
                rest,
            );
        }

        let duration_ms = i64::try_from(start.elapsed().as_millis()).unwrap_or(i64::MAX);
        let mut entries = Vec::new();
        if attempt > 1 || attempts > 1 {
            entries.extend(attempt_keys(attempt, attempts));
            entries.push(("duration_ms", PyJson::Int(duration_ms)));
        }
        if config.expected_status.is_some() {
            entries.push(("status_code", PyJson::Int(status_code)));
        }
        if config.expected_location.is_some() {
            entries.push(("location", location.map_or(PyJson::Null, PyJson::from)));
        }
        return result(check, ResultStatus::Ok, entries);
    }
    error(
        check,
        "unexpected_error",
        "HTTP check failed".to_owned(),
        attempt_keys(attempts, attempts),
    )
}

#[cfg(test)]
mod tests;
