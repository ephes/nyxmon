//! `notification.py:AsyncTelegramNotifier`: the Telegram alert (MarkdownV2,
//! resent once as plain text when Telegram cannot parse it) and the OpsGate
//! ticket submit whose outcome is appended to the alert.
//!
//! Security rule (PLAN.md 1.7): the notifier credentials never reach a log line or an
//! error string. Transport errors never include the request URL, and the
//! token is replaced by `<redacted>` in every line logged, including
//! response bodies an endpoint echoes back (OpsGate's included, where Python
//! does not redact).

use std::fmt;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use num_bigint::BigInt;
use nyxmon_core::env::NotifierEnv;
use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyDict, PyJson, loads_bytes};

use crate::http::{HttpClient, form_urlencode, timeout_from_secs};
use crate::log::{Level, NotifyLog, TracingLog};
use crate::markdown::{
    alert_body, append_ticket_notice, markdown_v2_to_plain_text, ticket_requested,
};
use crate::opsgate::{TicketOutcome, build_ticket_payload, encode_payload, task_ref, ticket_id};
use crate::pystr::py_repr;
use crate::{Delivery, Notifier, NotifyFuture, NotifyRaised};

/// Telegram's Bot API base URL.
pub const TELEGRAM_API_BASE: &str = "https://api.telegram.org";
/// `timeout=10.0` of the Telegram request.
pub const TELEGRAM_TIMEOUT: Duration = Duration::from_secs(10);
/// Lower-cased fragment of Telegram's HTTP 400 description for a body it
/// could not parse as MarkdownV2.
const PARSE_ERROR_MARKER: &str = "can't parse entities";
/// Longest response or error detail logged (code points).
const MAX_DETAIL_CHARS: usize = 500;

/// Wall clock in Unix microseconds (`datetime.now(tz=UTC)`).
pub type Clock = Arc<dyn Fn() -> i64 + Send + Sync>;
/// Source of the 16 hex digit prompt boundary (`secrets.token_hex(8)`).
pub type NonceSource = Arc<dyn Fn() -> String + Send + Sync>;

/// The system clock in Unix microseconds.
pub fn system_clock_micros() -> i64 {
    let since = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    i64::try_from(since.as_micros()).unwrap_or(i64::MAX)
}

/// `secrets.token_hex(8)`.
pub fn random_nonce() -> String {
    format!("{:016x}", rand::random::<u64>())
}

/// What the environment does not decide; tests inject the clock, the nonce,
/// the log and a local Telegram endpoint.
#[derive(Clone)]
pub struct NotifierSettings {
    pub telegram_api_base: String,
    pub telegram_timeout: Duration,
    pub clock: Clock,
    pub nonce: NonceSource,
    pub log: Arc<dyn NotifyLog>,
}

impl Default for NotifierSettings {
    fn default() -> Self {
        NotifierSettings {
            telegram_api_base: TELEGRAM_API_BASE.to_owned(),
            telegram_timeout: TELEGRAM_TIMEOUT,
            clock: Arc::new(system_clock_micros),
            nonce: Arc::new(random_nonce),
            log: Arc::new(TracingLog),
        }
    }
}

/// Outcome of one `_post_message`.
enum Post {
    Delivered,
    Failed,
    /// HTTP 400 "can't parse entities" for a MarkdownV2 body.
    ParseError,
}

/// `AsyncTelegramNotifier`.
pub struct TelegramNotifier {
    /// Set and non-empty, else `None` (Python treats `""` as missing).
    token: Option<String>,
    chat_id: Option<String>,
    /// `https://api.telegram.org/bot<token>/sendMessage`, empty without a token.
    url: String,
    /// Raw and encoded forms of all notifier-owned credentials.
    secrets: Vec<String>,
    opsgate_submit_base_url: String,
    opsgate_submit_token: String,
    opsgate_approval_base_url: String,
    opsgate_ticket_expires_seconds: BigInt,
    opsgate_include_warnings: bool,
    opsgate_timeout: Duration,
    telegram_client: HttpClient,
    opsgate_client: HttpClient,
    settings: NotifierSettings,
}

impl fmt::Debug for TelegramNotifier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TelegramNotifier")
            .field("token", &self.token.as_ref().map(|_| "<redacted>"))
            .field("chat_id", &self.chat_id)
            .field("opsgate_submit_base_url", &self.opsgate_submit_base_url)
            .field("opsgate_approval_base_url", &self.opsgate_approval_base_url)
            .finish_non_exhaustive()
    }
}

fn non_empty(value: &Option<String>) -> Option<String> {
    value.as_ref().filter(|v| !v.is_empty()).cloned()
}

impl TelegramNotifier {
    /// `AsyncTelegramNotifier.__init__` from the environment read by
    /// [`nyxmon_core::env::notifier_env`]. Warns when the token or the chat
    /// is missing.
    pub fn new(env: &NotifierEnv, settings: NotifierSettings) -> Self {
        let token = non_empty(&env.telegram_token);
        let chat_id = non_empty(&env.telegram_chat_id);
        if token.is_none() || chat_id.is_none() {
            settings.log.log(
                Level::Warning,
                "Telegram notifier initialized without token or chat_id",
            );
        }
        let url = token.as_ref().map_or_else(String::new, |token| {
            format!("{}/bot{token}/sendMessage", settings.telegram_api_base)
        });
        let mut secrets = Vec::new();
        for secret in token
            .iter()
            .chain(std::iter::once(&env.opsgate_submit_token))
        {
            if secret.is_empty() {
                continue;
            }
            secrets.push(secret.clone());
            if let Some(encoded) = url_encoded_token(secret) {
                secrets.push(encoded);
            }
            let form = form_urlencode(&[("", secret)]);
            let encoded = form.trim_start_matches('=');
            secrets.push(encoded.to_owned());
            secrets.push(encoded.replace('+', "%20"));
            // JSON/diagnostic quoting must not hide credentials from scrubbing.
            for rendered in [
                py_repr(&PyJson::from(secret.as_str())),
                PyJson::from(secret.as_str())
                    .to_json_string()
                    .unwrap_or_default(),
            ] {
                if rendered.len() >= 2 {
                    secrets.push(rendered[1..rendered.len() - 1].to_owned());
                }
            }
        }
        // Scrub longer forms first when one credential is a prefix of another.
        secrets.sort_by_key(|secret| std::cmp::Reverse(secret.len()));
        secrets.dedup();
        let opsgate_timeout = timeout_from_secs(env.opsgate_submit_timeout_seconds);
        TelegramNotifier {
            telegram_client: HttpClient::new(settings.telegram_timeout),
            opsgate_client: HttpClient::new(opsgate_timeout),
            token,
            chat_id,
            url,
            secrets,
            opsgate_submit_base_url: env.opsgate_submit_base_url.clone(),
            opsgate_submit_token: env.opsgate_submit_token.clone(),
            opsgate_approval_base_url: env.opsgate_approval_base_url.clone(),
            opsgate_ticket_expires_seconds: env.opsgate_ticket_expires_seconds.clone(),
            opsgate_include_warnings: env.opsgate_include_warnings,
            opsgate_timeout,
            settings,
        }
    }

    /// The timeout of the Telegram request.
    pub fn telegram_timeout(&self) -> Duration {
        self.settings.telegram_timeout
    }

    /// The timeout of the OpsGate submit (`OPSGATE_SUBMIT_TIMEOUT_SECONDS`).
    pub fn opsgate_timeout(&self) -> Duration {
        self.opsgate_timeout
    }

    /// Every line goes through [`Self::redact`]: no log line may carry the
    /// notifier credentials, whatever a remote endpoint echoes back.
    fn log(&self, level: Level, message: &str) {
        self.settings.log.log(level, &self.redact(message));
    }

    /// Replace raw and encoded notifier credentials by `<redacted>`.
    fn redact(&self, text: &str) -> String {
        let mut text = text.to_owned();
        for secret in &self.secrets {
            text = text.replace(secret.as_str(), "<redacted>");
        }
        text
    }

    /// `async_notify_check_failed`.
    /// Optional ticket failures never suppress the primary Telegram alert.
    pub async fn notify(
        &self,
        check: &Check,
        result: &CheckResult,
    ) -> Result<Delivery, NotifyRaised> {
        let critical = result.status == ResultStatus::Error;
        let mut message = alert_body(check, result);
        if ticket_requested(check, result, self.opsgate_include_warnings)
            && let Some(ticket) = self.create_ticket(check, result).await?
        {
            append_ticket_notice(&mut message, &ticket, &self.opsgate_approval_base_url);
        }
        Ok(self.send(&message, critical).await)
    }

    /// `async_send`: [`Delivery::Unknown`] without credentials, otherwise the
    /// MarkdownV2 send with one plain-text resend on a parse error.
    pub async fn send(&self, text: &str, high_priority: bool) -> Delivery {
        let (Some(_), Some(_)) = (&self.token, &self.chat_id) else {
            self.log(
                Level::Warning,
                "Cannot send Telegram notification: missing credentials",
            );
            return Delivery::Unknown;
        };
        match self.post_message(text, high_priority, true).await {
            Post::Delivered => Delivery::Delivered,
            Post::Failed => Delivery::Failed,
            Post::ParseError => {
                // Formatting must never cost an alert.
                self.log(
                    Level::Warning,
                    "Telegram rejected the MarkdownV2 body; resending as plain text",
                );
                match self
                    .post_message(&markdown_v2_to_plain_text(text), high_priority, false)
                    .await
                {
                    Post::Delivered => Delivery::Delivered,
                    Post::Failed | Post::ParseError => Delivery::Failed,
                }
            }
        }
    }

    /// `_post_message`.
    async fn post_message(&self, text: &str, high_priority: bool, markdown: bool) -> Post {
        let chat_id = self.chat_id.as_deref().unwrap_or_default();
        let disable_notification = if high_priority { "false" } else { "true" };
        let mut fields = vec![
            ("chat_id", chat_id),
            ("text", text),
            ("disable_notification", disable_notification),
        ];
        if markdown {
            fields.push(("parse_mode", "MarkdownV2"));
        }
        let sent = self
            .telegram_client
            .post(
                &self.url,
                &[("content-type", "application/x-www-form-urlencoded")],
                form_urlencode(&fields),
            )
            .await;
        match sent {
            Ok(response) if (200..300).contains(&response.status) => {
                if !telegram_acknowledged(&response.body) {
                    // The provider may have sent the message: leave retries to
                    // the existing caller policy, without an immediate resend.
                    self.log(Level::Error, "Telegram notification was not acknowledged: invalid or negative success envelope");
                    return Post::Failed;
                }
                self.log(Level::Info, "Telegram notification delivered");
                Post::Delivered
            }
            Ok(response) => {
                let status = response.status;
                let text = String::from_utf8_lossy(&response.body);
                if markdown && status == 400 && text.to_lowercase().contains(PARSE_ERROR_MARKER) {
                    return Post::ParseError;
                }
                let detail: String = self.redact(&text).chars().take(MAX_DETAIL_CHARS).collect();
                self.log(
                    Level::Error,
                    &format!(
                        "Failed to send Telegram notification: HTTP status {status} body={detail}"
                    ),
                );
                Post::Failed
            }
            Err(err) => {
                let class = err.class;
                let detail: String = self
                    .redact(&err.detail)
                    .chars()
                    .take(MAX_DETAIL_CHARS)
                    .collect();
                self.log(
                    Level::Error,
                    &format!("Failed to send Telegram notification: {class}: {detail}"),
                );
                Post::Failed
            }
        }
    }

    /// `_create_opsgate_ticket`: `None` when OpsGate is not configured.
    async fn create_ticket(
        &self,
        check: &Check,
        result: &CheckResult,
    ) -> Result<Option<TicketOutcome>, NotifyRaised> {
        if self.opsgate_submit_base_url.is_empty() || self.opsgate_submit_token.is_empty() {
            return Ok(None);
        }
        let payload = match build_ticket_payload(
            check,
            result,
            (self.settings.clock)(),
            &self.opsgate_ticket_expires_seconds,
            &(self.settings.nonce)(),
        ) {
            Ok(payload) => payload,
            Err(_) => {
                self.log(Level::Error, &format!(
                    "OpsGate ticket payload construction failed for check_id={}: invalid expiry",
                    check.check_id
                ));
                return Ok(Some(TicketOutcome::Error));
            }
        };
        let check_id = check.check_id;
        let submit_failed = |detail: &str| {
            let detail: String = self.redact(detail).chars().take(MAX_DETAIL_CHARS).collect();
            self.log(
                Level::Error,
                &format!("OpsGate ticket submit failed for check_id={check_id}: {detail}"),
            );
            Ok(Some(TicketOutcome::Error))
        };
        // httpx encodes `json=` before sending; a non-finite float fails the
        // submit (ValueError) without a request.
        let body = match encode_payload(&payload) {
            Ok(body) => body,
            Err(message) => return submit_failed(&message),
        };
        let authorization = format!("Bearer {}", self.opsgate_submit_token);
        let sent = self
            .opsgate_client
            .post(
                &format!("{}/api/v1/tickets", self.opsgate_submit_base_url),
                &[
                    ("content-type", "application/json"),
                    ("authorization", &authorization),
                ],
                body,
            )
            .await;
        let (status, raw) = match sent {
            Ok(response) => (response.status, response.body),
            Err(err) => return submit_failed(&err.detail),
        };
        let body: PyDict = loads_bytes(&raw)
            .ok()
            .and_then(|parsed| parsed.into_dict().ok())
            .unwrap_or_default();
        if status == 201 {
            let Some(ticket_id) = body.get("id").and_then(ticket_id) else {
                self.log(
                    Level::Error,
                    &format!("OpsGate ticket submit succeeded with invalid ticket id for check_id={check_id}"),
                );
                return Ok(Some(TicketOutcome::Error));
            };
            return Ok(Some(TicketOutcome::Created(ticket_id)));
        }
        if status == 409
            && matches!(body.get("error"), Some(PyJson::Str(e)) if e == "duplicate_open_ticket")
        {
            self.log(
                Level::Info,
                &format!(
                    "OpsGate duplicate open ticket ignored for check_id={check_id} task_ref={}",
                    task_ref(check, result)
                ),
            );
            return Ok(Some(TicketOutcome::Duplicate));
        }
        self.log(
            Level::Error,
            &format!(
                "OpsGate ticket submit returned status={status} for check_id={check_id} body={}",
                self.redact(&py_repr(&PyJson::Dict(body)))
                    .chars()
                    .take(MAX_DETAIL_CHARS)
                    .collect::<String>()
            ),
        );
        Ok(Some(TicketOutcome::Error))
    }
}

/// `sendMessage` returns an `ok: true` envelope containing a Message.
/// Strict JSON parsing has its own nesting bound; HTTP caps bytes at 64 KiB.
fn telegram_acknowledged(body: &[u8]) -> bool {
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(body) else {
        return false;
    };
    value.get("ok").and_then(serde_json::Value::as_bool) == Some(true)
        && value
            .get("result")
            .and_then(|result| result.get("message_id"))
            .and_then(serde_json::Value::as_u64)
            .is_some_and(|id| id > 0)
}

/// The token as it appears in the request URL path (percent-encoded).
fn url_encoded_token(token: &str) -> Option<String> {
    let url = url::Url::parse(&format!("http://localhost/bot{token}/sendMessage")).ok()?;
    url.path()
        .strip_prefix("/bot")
        .and_then(|rest| rest.strip_suffix("/sendMessage"))
        .map(str::to_owned)
}

impl Notifier for TelegramNotifier {
    fn notify_check_failed<'a>(
        &'a self,
        check: &'a Check,
        result: &'a CheckResult,
    ) -> NotifyFuture<'a> {
        Box::pin(self.notify(check, result))
    }
}
