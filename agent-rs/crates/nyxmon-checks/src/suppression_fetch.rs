//! The maintenance-suppression fetch (`notification_suppression.py:
//! _fetch_payload` and `_read_capped_body`, PLAN.md 5.4).
//!
//! Python creates one `httpx.Client` per fetch (no cookies shared with
//! other fetches or the batch; cookies set by a redirect hop are sent on the
//! next hop of the same fetch), sends `Accept-Encoding: identity`, follows
//! redirects by hand without reading their bodies (at most
//! [`MAX_SUPPRESSION_REDIRECTS`] hops, `TooManyRedirects` after that), keeps
//! the `Authorization` header only on same-origin redirects and https
//! upgrades (`next_request` via `_redirect_headers`), refuses any
//! `Content-Encoding` other than identity, caps the body at
//! [`MAX_SUPPRESSION_BODY_BYTES`] (declared and streamed), and decodes it
//! with `json.loads`. Every failure means "no payload": the caller fails
//! open.
//!
//! The requests use the httpx timeout semantics of [`HttpSession`]
//! (connect, every read and every write bounded separately).

use hyper::header::{self, HeaderValue};
use num_bigint::BigInt;
use nyxmon_core::pyjson::{PyJson, loads_bytes, py_int, py_lower, py_strip};
use nyxmon_core::suppression::{
    MAX_SUPPRESSION_BODY_BYTES, MAX_SUPPRESSION_REDIRECTS, SuppressionRequest,
};

use crate::http_client::{
    BodyLimit, HttpSession, basic_authorization, header_text, keeps_authorization, read_body,
    redirect_target, request_url, timeout_duration,
};
use crate::httpx_url::HttpxUrl;

/// `_build_request_auth` without client auth: the `auth=` of the send, else
/// credentials in the URL.
fn request_auth(auth: Option<&(String, String)>, url: &HttpxUrl) -> Option<HeaderValue> {
    if let Some((username, password)) = auth {
        return Some(basic_authorization(username, password));
    }
    let (username, password) = (url.username(), url.password());
    (!username.is_empty() || !password.is_empty())
        .then(|| basic_authorization(&username, &password))
}

/// `_fetch_payload(url, timeout=..., auth=...)` on a fresh session: the
/// decoded payload, or `None` on any failure.
pub async fn fetch_payload(request: &SuppressionRequest) -> Option<PyJson> {
    fetch_payload_with(&HttpSession::new(), request).await
}

/// [`fetch_payload`] on a given session (tests route host names to fake
/// servers). The session must be fresh: its cookie jar is the fetch's.
pub async fn fetch_payload_with(
    session: &HttpSession,
    request: &SuppressionRequest,
) -> Option<PyJson> {
    // `socket.settimeout(nan)` raises ValueError before connecting.
    if request.timeout.is_nan() {
        return None;
    }
    let timeout = timeout_duration(request.timeout);
    let mut url = request_url(&request.url).ok()?;
    let identity = [(
        header::ACCEPT_ENCODING,
        HeaderValue::from_static("identity"),
    )];
    // The `Authorization` header carried over from the previous hop.
    let mut carried: Option<HeaderValue> = None;
    let mut hop_auth = request.auth.as_ref();
    for _ in 0..=MAX_SUPPRESSION_REDIRECTS {
        let authorization = request_auth(hop_auth, &url).or_else(|| carried.clone());
        let response = session
            .send_once(&url, timeout, authorization.as_ref(), &identity)
            .await
            .ok()?;
        if let Some(next) = redirect_target(&url, &response).ok()? {
            // The redirect body is never read: dropping the response closes
            // the connection.
            drop(response);
            carried = authorization.filter(|_| keeps_authorization(&url, &next));
            hop_auth = None;
            url = next;
            continue;
        }
        if !(200..300).contains(&response.status) {
            return None;
        }
        let encoding = header_text(&response.headers, header::CONTENT_ENCODING)
            .map_or_else(|| "identity".to_owned(), |value| py_lower(py_strip(&value)));
        if !encoding.is_empty() && encoding != "identity" {
            return None;
        }
        let cap = BigInt::from(MAX_SUPPRESSION_BODY_BYTES);
        if let Some(declared) = header_text(&response.headers, header::CONTENT_LENGTH)
            && py_int(&declared).is_ok_and(|n| n > cap)
        {
            return None;
        }
        let body = read_body(response, Some(BodyLimit { limit: &cap }))
            .await
            .ok()?;
        return loads_bytes(&body).ok();
    }
    // httpx.TooManyRedirects
    None
}

#[cfg(test)]
mod tests;
