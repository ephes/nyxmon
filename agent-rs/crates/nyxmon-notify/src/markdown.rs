//! The Telegram alert text (`notification.py:async_notify_check_failed`) and
//! the MarkdownV2 helpers (`escape_markdown_v2`, `markdown_v2_to_plain_text`,
//! `_escape_markdown_link_url`).

use nyxmon_core::model::{Check, CheckResult, ResultStatus};
use nyxmon_core::pyjson::{PyJson, py_truthy};

use crate::opsgate::TicketOutcome;
use crate::pystr::py_str;
use crate::sanitize::truncate_chars;

/// `ALERT_MAX_NAME_CHARS`.
pub const ALERT_MAX_NAME_CHARS: usize = 200;
/// `ALERT_MAX_URL_CHARS`.
pub const ALERT_MAX_URL_CHARS: usize = 500;
/// `ALERT_MAX_ERROR_TYPE_CHARS` (also the cap of the status line).
pub const ALERT_MAX_ERROR_TYPE_CHARS: usize = 100;
/// `ALERT_MAX_ERROR_MSG_CHARS`.
pub const ALERT_MAX_ERROR_MSG_CHARS: usize = 2000;

/// Characters MarkdownV2 requires to be escaped outside entities, plus the
/// backslash itself.
const SPECIAL: &str = "\\_*[]()~`>#+-=|{}.!";
/// Unescaped MarkdownV2 characters that only switch formatting on or off.
const FORMATTING: &str = "*_~`|";

/// `AsyncTelegramNotifier.escape_markdown_v2`: a backslash before every
/// special character, the backslash included.
pub fn escape_markdown_v2(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 8);
    for c in text.chars() {
        if SPECIAL.contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// `markdown_v2_to_plain_text`: escapes removed, formatting markers dropped,
/// `[label](url)` rendered as `label (url)`.
pub fn markdown_v2_to_plain_text(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut in_link_url = false;
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '\\' && i + 1 < chars.len() {
            out.push(chars[i + 1]);
            i += 2;
            continue;
        }
        if in_link_url {
            if c == ')' {
                in_link_url = false;
            }
            out.push(c);
        } else if c == ']' && chars.get(i + 1) == Some(&'(') {
            out.push_str(" (");
            in_link_url = true;
            i += 2;
            continue;
        } else if c == '[' {
        } else if !FORMATTING.contains(c) {
            out.push(c);
        }
        i += 1;
    }
    out
}

/// `_escape_markdown_link_url`: only `\` and `)` are special inside a link
/// target.
pub fn escape_markdown_link_url(url: &str) -> String {
    url.replace('\\', "\\\\").replace(')', "\\)")
}

/// `escape_markdown_v2(_truncate(str(value), cap))`.
fn field(value: &str, cap: usize) -> String {
    escape_markdown_v2(&truncate_chars(value, cap))
}

/// The alert body before the OpsGate addendum.
pub fn alert_body(check: &Check, result: &CheckResult) -> String {
    let data = &result.data;
    let error_msg = data
        .get("error_msg")
        .map_or_else(|| "Unknown error".to_owned(), py_str);
    let error_type = data.get("error_type");
    let status_code = data.get("status_code");

    let mut message = String::new();
    if result.status == ResultStatus::Error {
        message.push_str("\u{1f534} *Check Failed \\(Critical\\)*\n");
    } else {
        message.push_str("\u{26a0}\u{fe0f} *Check Warning*\n");
    }
    let name = if check.name.is_empty() {
        "Unnamed Check".to_owned()
    } else {
        field(&check.name, ALERT_MAX_NAME_CHARS)
    };
    message.push_str(&format!("Name: {name}\n"));
    message.push_str(&format!(
        "URL: {}\n",
        field(&check.url, ALERT_MAX_URL_CHARS)
    ));
    if let Some(status) = status_code.filter(|v| py_truthy(v)) {
        message.push_str(&format!(
            "Status: {}\n",
            field(&py_str(status), ALERT_MAX_ERROR_TYPE_CHARS)
        ));
    }
    if let Some(kind) = error_type.filter(|v| py_truthy(v)) {
        message.push_str(&format!(
            "Error Type: {}\n",
            field(&py_str(kind), ALERT_MAX_ERROR_TYPE_CHARS)
        ));
    }
    message.push_str(&format!(
        "Error: {}",
        field(&error_msg, ALERT_MAX_ERROR_MSG_CHARS)
    ));
    message
}

/// `ticket_wanted`: only Nyxmon's internal collector row may suppress a
/// ticket, with a literal `opsgate_ticket: false`.
pub fn ticket_wanted(check: &Check, result: &CheckResult) -> bool {
    !(crate::opsgate::is_internal_alert(check)
        && matches!(result.data.get("opsgate_ticket"), Some(PyJson::Bool(false))))
}

/// Whether `async_notify_check_failed` asks OpsGate for a ticket: wanted, and
/// an error or (with `OPSGATE_SUBMIT_INCLUDE_WARNINGS`) a warning.
pub fn ticket_requested(check: &Check, result: &CheckResult, include_warnings: bool) -> bool {
    ticket_wanted(check, result) && (result.status == ResultStatus::Error || include_warnings)
}

/// Append the OpsGate outcome to the alert body.
pub fn append_ticket_notice(message: &mut String, ticket: &TicketOutcome, approval_base: &str) {
    match ticket {
        TicketOutcome::Created(ticket_id) => {
            let approval_url = format!("{approval_base}/tickets/{ticket_id}");
            message.push_str("\n\n\u{1f6e0} *OpsGate Approval Needed*");
            message.push_str(&format!("\nTicket: `{}`", escape_markdown_v2(ticket_id)));
            message.push_str(&format!(
                "\n[Open approval page]({})",
                escape_markdown_link_url(&approval_url)
            ));
        }
        TicketOutcome::Duplicate => {
            message.push_str("\n\n\u{1f6e0} *OpsGate*");
            message.push_str("\nAn open remediation ticket already exists for this check\\.");
        }
        TicketOutcome::Error => {
            message.push_str("\n\n\u{1f6e0} *OpsGate*");
            message.push_str("\nTicket creation failed; please create one manually\\.");
        }
    }
}
