//! The httpx rules of the shared HTTP layer (PLAN.md 5.5), one fake-server
//! test per redirect, decoding, cookie and timeout rule.

use std::sync::Arc;

use super::*;
use crate::testutil::{Action, FakeServer, head, redirect, respond};

fn session() -> HttpSession {
    HttpSession::new()
}

fn get(url: &str, follow: bool) -> GetRequest<'_> {
    GetRequest {
        url,
        timeout: Some(Duration::from_secs(5)),
        follow_redirects: follow,
        authorization: None,
        timeout_overflows: false,
    }
}

fn parse(url: &str) -> HttpxUrl {
    HttpxUrl::parse(url).unwrap()
}

#[tokio::test]
async fn requests_carry_the_httpx_default_headers() {
    let server = FakeServer::start(vec![respond(200, &[], b"")]).await;
    let url = format!("http://127.0.0.1:{}/x/%2e%2e/y?z=%2e", server.port());
    session().get(&get(&url, true)).await.unwrap();
    let request = &server.requests()[0];
    assert_eq!(request.target, "/x/%2e%2e/y?z=%2e");
    assert_eq!(
        request.header("host"),
        Some(format!("127.0.0.1:{}", server.port()).as_str())
    );
    assert_eq!(request.header("user-agent"), Some("python-httpx/0.28.1"));
    assert_eq!(request.header("accept"), Some("*/*"));
    assert_eq!(request.header("accept-encoding"), Some("gzip, deflate"));
    assert_eq!(request.header("connection"), Some("keep-alive"));
}

#[tokio::test]
async fn only_redirect_statuses_with_a_location_are_followed() {
    for status in [301, 302, 303, 307, 308] {
        let server =
            FakeServer::start(vec![redirect(status, "/next"), respond(204, &[], b"")]).await;
        let url = format!("http://127.0.0.1:{}/", server.port());
        let response = session().get(&get(&url, true)).await.unwrap();
        assert_eq!(response.status, 204, "{status}");
        let targets: Vec<String> = server.requests().into_iter().map(|r| r.target).collect();
        assert_eq!(targets, ["/", "/next"], "{status}");
    }
    for status in [300, 304, 305] {
        let server = FakeServer::start(vec![redirect(status, "/next")]).await;
        let url = format!("http://127.0.0.1:{}/", server.port());
        let response = session().get(&get(&url, true)).await.unwrap();
        assert_eq!(response.status, status);
        assert_eq!(server.requests().len(), 1);
    }
    let server = FakeServer::start(vec![respond(302, &[], b"")]).await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let response = session().get(&get(&url, true)).await.unwrap();
    assert_eq!(response.status, 302);
}

#[test]
fn authorization_survives_same_origin_and_https_upgrades_only() {
    let keep = [
        ("http://a.test/x", "http://a.test/y"),
        ("http://a.test/x", "http://a.test:80/y"),
        ("http://a.test/x", "https://a.test/y"),
        ("http://a.test:80/x", "https://a.test:443/y"),
        ("https://a.test/x", "https://a.test:443/y"),
        ("http://A.test/x", "http://a.TEST/y"),
    ];
    for (from, to) in keep {
        assert!(
            keeps_authorization(&parse(from), &parse(to)),
            "{from} -> {to}"
        );
    }
    let drop = [
        ("http://a.test/x", "http://b.test/y"),
        ("http://a.test/x", "http://a.test:8080/y"),
        ("https://a.test/x", "http://a.test/y"),
        ("http://a.test:8080/x", "https://a.test/y"),
        ("http://a.test/x", "https://a.test:8443/y"),
    ];
    for (from, to) in drop {
        assert!(
            !keeps_authorization(&parse(from), &parse(to)),
            "{from} -> {to}"
        );
    }
}

#[tokio::test]
async fn authorization_is_dropped_on_a_cross_origin_redirect() {
    let other = FakeServer::start(vec![respond(200, &[], b"")]).await;
    let first = FakeServer::start(vec![
        redirect(302, "/same"),
        redirect(302, &format!("http://127.0.0.1:{}/other", other.port())),
    ])
    .await;
    let url = format!("http://127.0.0.1:{}/", first.port());
    let auth = basic_authorization("user", "pw");
    let request = GetRequest {
        authorization: Some(auth.clone()),
        ..get(&url, true)
    };
    session().get(&request).await.unwrap();
    let first_requests = first.requests();
    assert_eq!(
        first_requests[0].header("authorization"),
        auth.to_str().ok()
    );
    assert_eq!(
        first_requests[1].header("authorization"),
        auth.to_str().ok()
    );
    assert_eq!(other.requests()[0].header("authorization"), None);
}

#[tokio::test]
async fn url_credentials_are_basic_auth_kept_like_explicit_auth() {
    let server = FakeServer::start(vec![]).await;
    let port = server.port();
    server.set_script(vec![
        redirect(302, &format!("http://127.0.0.1:{port}/same")),
        respond(200, &[], b""),
    ]);
    let url = format!("http://us%65r:p%40ss@127.0.0.1:{}/start", server.port());
    session().get(&get(&url, true)).await.unwrap();
    let expected = basic_authorization("user", "p@ss");
    let requests = server.requests();
    assert_eq!(requests[0].header("authorization"), expected.to_str().ok());
    // The absolute redirect carries no credentials, but it is same-origin.
    assert_eq!(requests[1].header("authorization"), expected.to_str().ok());
    // An explicit auth= wins over the URL credentials.
    let server = FakeServer::start(vec![respond(200, &[], b"")]).await;
    let url = format!("http://u:p@127.0.0.1:{}/", server.port());
    let explicit = basic_authorization("x", "y");
    let request = GetRequest {
        authorization: Some(explicit.clone()),
        ..get(&url, true)
    };
    session().get(&request).await.unwrap();
    assert_eq!(
        server.requests()[0].header("authorization"),
        explicit.to_str().ok()
    );
}

#[tokio::test]
async fn a_corrupt_redirect_body_is_a_request_error() {
    let step = respond(
        302,
        &[("Location", "/next"), ("Content-Encoding", "gzip")],
        b"not gzip at all",
    );
    let server = FakeServer::start(vec![step]).await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let err = session().get(&get(&url, true)).await.unwrap_err();
    assert_eq!(err.kind, HttpErrorKind::Request);
    assert_eq!(server.requests().len(), 1);
}

#[tokio::test]
async fn cookies_are_shared_by_every_request_of_a_session() {
    let server = FakeServer::start(vec![
        respond(
            302,
            &[("Location", "/next"), ("Set-Cookie", "a=1; Path=/")],
            b"",
        ),
        respond(200, &[("Set-Cookie", "b=2; Path=/")], b""),
        respond(200, &[], b""),
        respond(200, &[], b""),
    ])
    .await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let shared = session();
    shared.get(&get(&url, true)).await.unwrap();
    // Another check of the batch, with another timeout.
    let other_timeout = GetRequest {
        timeout: Some(Duration::from_secs(7)),
        ..get(&url, true)
    };
    shared.get(&other_timeout).await.unwrap();
    // A new batch starts without cookies.
    session().get(&get(&url, true)).await.unwrap();
    let cookies: Vec<Option<String>> = server
        .requests()
        .iter()
        .map(|r| r.header("cookie").map(str::to_owned))
        .collect();
    assert_eq!(
        cookies,
        [
            None,
            Some("a=1".to_owned()),
            Some("a=1; b=2".to_owned()),
            None
        ]
    );
}

#[tokio::test]
async fn cookies_are_sent_longest_path_first_then_in_insertion_order() {
    for _ in 0..20 {
        let server = FakeServer::start(vec![
            respond(
                302,
                &[
                    ("Location", "/deep/x"),
                    ("Set-Cookie", "z=1; Path=/"),
                    ("Set-Cookie", "sid=root; Path=/"),
                    ("Set-Cookie", "sid=deep; Path=/deep"),
                    ("Set-Cookie", "a=2; Path=/"),
                ],
                b"",
            ),
            respond(200, &[], b""),
        ])
        .await;
        let url = format!("http://127.0.0.1:{}/", server.port());
        session().get(&get(&url, true)).await.unwrap();
        assert_eq!(
            server.requests()[1].header("cookie"),
            Some("sid=deep; z=1; sid=root; a=2")
        );
    }
}

#[tokio::test]
async fn the_timeout_resets_with_every_read() {
    // Header pieces 200 ms apart with a 500 ms timeout: no single read waits
    // longer than the timeout, so the request succeeds (httpx per-read).
    let mut step = vec![];
    for piece in [
        "HTTP/1.1 200 OK\r\n",
        "X-A: 1\r\n",
        "X-B: 2\r\n",
        "Content-Length: 0\r\n",
        "\r\n",
    ] {
        step.push(Action::Sleep(Duration::from_millis(200)));
        step.push(Action::Write(piece.as_bytes().to_vec()));
    }
    let server = FakeServer::start(vec![step]).await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let request = GetRequest {
        timeout: Some(Duration::from_millis(500)),
        ..get(&url, true)
    };
    let response = session().get(&request).await.unwrap();
    assert_eq!(response.status, 200);

    // A single silence longer than the timeout fails as a timeout.
    let step = vec![
        Action::Sleep(Duration::from_millis(800)),
        Action::Write(head(200, &[("Content-Length", "0")])),
    ];
    let server = FakeServer::start(vec![step]).await;
    let url = format!("http://127.0.0.1:{}/", server.port());
    let request = GetRequest {
        timeout: Some(Duration::from_millis(300)),
        ..get(&url, true)
    };
    let err = session().get(&request).await.unwrap_err();
    assert_eq!(err.kind, HttpErrorKind::Timeout);
    assert_eq!(err.message, "");
}

#[tokio::test]
async fn a_timeout_beyond_float_range_fails_after_the_scheme_check() {
    let request = GetRequest {
        timeout_overflows: true,
        ..get("http://127.0.0.1:9/", true)
    };
    assert_eq!(
        session().get(&request).await.unwrap_err().kind,
        HttpErrorKind::Unexpected
    );
    let request = GetRequest {
        timeout_overflows: true,
        ..get("ftp://127.0.0.1:9/", true)
    };
    assert_eq!(
        session().get(&request).await.unwrap_err().kind,
        HttpErrorKind::Request
    );
}

#[tokio::test]
async fn declared_lengths_over_the_limit_fail_before_reading() {
    let body = b"[1, 2, 3, 4]";
    for (limit, too_large) in [(4, true), (12, false)] {
        let server = FakeServer::start(vec![respond(200, &[], body)]).await;
        let url = format!("http://127.0.0.1:{}/", server.port());
        let response = session().get(&get(&url, true)).await.unwrap();
        let limit = BigInt::from(limit);
        let result = read_body(response, Some(BodyLimit { limit: &limit })).await;
        if too_large {
            assert_eq!(
                result,
                Err(BodyError::TooLarge(
                    "response body of 12 bytes exceeds the 4 byte limit".to_owned()
                ))
            );
        } else {
            assert_eq!(result.unwrap(), body);
        }
    }
}

#[test]
fn header_text_decodes_like_httpx() {
    let mut headers = HeaderMap::new();
    headers.append(header::LOCATION, HeaderValue::from_static("/a"));
    headers.append(header::LOCATION, HeaderValue::from_static("/b"));
    assert_eq!(
        header_text(&headers, header::LOCATION).as_deref(),
        Some("/a, /b")
    );
    headers.insert(
        header::LOCATION,
        HeaderValue::from_bytes("/\u{e9}".as_bytes()).unwrap(),
    );
    assert_eq!(
        header_text(&headers, header::LOCATION).as_deref(),
        Some("/\u{e9}")
    );
    // One non-UTF-8 header switches the whole block to ISO-8859-1.
    headers.insert("x-raw", HeaderValue::from_bytes(b"\xff").unwrap());
    assert_eq!(
        header_text(&headers, header::LOCATION).as_deref(),
        Some("/\u{c3}\u{a9}")
    );
    let mut encodings = HeaderMap::new();
    encodings.append(
        header::CONTENT_ENCODING,
        HeaderValue::from_static("gzip, deflate"),
    );
    encodings.append(header::CONTENT_ENCODING, HeaderValue::from_static(" br"));
    assert_eq!(content_encodings(&encodings), ["gzip", "deflate", "br"]);
}

#[tokio::test]
async fn https_uses_the_configured_roots_and_http1() {
    use rcgen::{CertificateParams, KeyPair};
    use rustls::ServerConfig;
    use rustls::pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let key = KeyPair::generate().unwrap();
    let cert = CertificateParams::new(vec!["localhost".to_owned()])
        .unwrap()
        .self_signed(&key)
        .unwrap();
    let mut server_config = ServerConfig::builder_with_provider(crate::tls::crypto_provider())
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(
            vec![cert.der().clone()],
            PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.serialize_der())),
        )
        .unwrap();
    server_config.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server_config));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (tcp, _) = listener.accept().await.unwrap();
        let mut tls = acceptor.accept(tcp).await.unwrap();
        let alpn = tls.get_ref().1.alpn_protocol().map(<[u8]>::to_vec);
        let mut buf = vec![0u8; 4096];
        let _ = tls.read(&mut buf).await.unwrap();
        tls.write_all(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        tls.shutdown().await.ok();
        alpn
    });
    let mut roots = RootCertStore::empty();
    roots.add(cert.der().clone()).unwrap();
    let session = HttpSession::with_roots(Arc::new(roots)).resolve("localhost", addr);
    let url = format!("https://localhost:{}/", addr.port());
    let response = session.get(&get(&url, true)).await.unwrap();
    assert_eq!(response.status, 204);
    assert_eq!(server.await.unwrap(), Some(b"http/1.1".to_vec()));

    // The default session does not trust the self-signed certificate.
    let untrusted = HttpSession::new().resolve("localhost", addr);
    let err = untrusted.get(&get(&url, true)).await.unwrap_err();
    assert_eq!(err.kind, HttpErrorKind::Connect);
}

#[tokio::test]
async fn cookie_paths_match_the_path_as_sent() {
    // httpx sends /a/%2e%2e/b unresolved, so a cookie for /b does not apply.
    let server = FakeServer::start(vec![
        respond(200, &[("Set-Cookie", "c=1; Path=/b")], b""),
        respond(200, &[], b""),
        respond(200, &[], b""),
    ])
    .await;
    let shared = session();
    let base = format!("http://127.0.0.1:{}", server.port());
    shared.get(&get(&format!("{base}/b"), true)).await.unwrap();
    shared
        .get(&get(&format!("{base}/a/%2e%2e/b"), true))
        .await
        .unwrap();
    shared
        .get(&get(&format!("{base}/b/x"), true))
        .await
        .unwrap();
    let cookies: Vec<Option<String>> = server
        .requests()
        .iter()
        .map(|r| r.header("cookie").map(str::to_owned))
        .collect();
    assert_eq!(cookies, [None, None, Some("c=1".to_owned())]);
    assert_eq!(server.requests()[1].target, "/a/%2e%2e/b");
}

#[tokio::test]
async fn cookie_scopes_keep_escaped_paths_distinct() {
    let server = FakeServer::start(vec![
        // Default path /a/%2E (no Path attribute).
        respond(200, &[("Set-Cookie", "d=1")], b""),
        respond(200, &[("Set-Cookie", "f=2; Path=/file%2etxt")], b""),
        respond(200, &[], b""),
        respond(200, &[], b""),
        respond(200, &[], b""),
        respond(200, &[("Set-Cookie", "b=3; Path=/a%5cb")], b""),
        respond(200, &[], b""),
        respond(200, &[], b""),
    ])
    .await;
    let shared = session();
    let base = format!("http://127.0.0.1:{}", server.port());
    let paths = [
        "/a/%2E/start",
        "/x",
        "/a/%252E/x",
        "/a/%2e/y",
        "/file%2Etxt",
        "/s",
        "/a\\b/x",
        "/a%5Cb|x",
    ];
    for path in paths {
        let url = format!("{base}{path}");
        shared.get(&get(&url, true)).await.unwrap();
    }
    let cookies: Vec<Option<&str>> = server
        .requests()
        .iter()
        .map(|r| {
            r.header("cookie").map(|c| match c {
                "d=1" => "d=1",
                "f=2" => "f=2",
                "b=3" => "b=3",
                _ => "unexpected",
            })
        })
        .collect();
    // Recorded from httpx against a local server: a literal %25 path is
    // another path, escapes match case-insensitively and a raw backslash
    // equals %5C (http.cookiejar's escape_path).
    let expected = [
        None,
        None,
        None,
        Some("d=1"),
        Some("f=2"),
        None,
        Some("b=3"),
        None,
    ];
    assert_eq!(cookies, expected);
}

#[test]
fn cookie_paths_follow_escape_path() {
    assert_eq!(
        escape_path("/a\\b|[^]%2e \u{e9}"),
        "/a%5Cb%7C%5B%5E%5D%2E%20%C3%A9"
    );
    assert_eq!(cookie_path("/a/%2e%2E/b"), "/a/%252E%252E/b");
    assert_eq!(cookie_path("/a/%252E"), "/a/%25252E");
    // A raw backslash and its escape are the same cookie path in Python.
    assert_eq!(cookie_path("/a\\b"), cookie_path("/a%5cb"));
}
