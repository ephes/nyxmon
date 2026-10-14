//! httpx URL parsing and redirect resolution against
//! `fixtures/httpx_url.json` (generated with httpx's `URL` and
//! `Client._redirect_url`).

use nyxmon_core::pyjson::{PyJson, loads};

use super::*;
use crate::http_client::{HttpErrorKind, redirect_url};

fn fixture() -> PyJson {
    loads(include_str!("../../../../fixtures/httpx_url.json")).unwrap()
}

fn text<'a>(case: &'a PyJson, key: &str) -> &'a str {
    case.get(key)
        .and_then(PyJson::as_str)
        .unwrap_or_else(|| panic!("{key}"))
}

#[test]
fn parsing_matches_httpx() {
    let fixture = fixture();
    let cases = fixture.get("parse").unwrap().as_list().unwrap();
    for case in cases {
        let input = text(case, "url");
        match HttpxUrl::parse(input) {
            Err(err) => assert!(case.get("error").is_some(), "{input:?}: {err}"),
            Ok(url) => {
                assert!(
                    case.get("error").is_none(),
                    "{input:?} must be invalid: {url:?}"
                );
                assert_eq!(url.to_string(), text(case, "str"), "{input:?}");
                assert_eq!(url.scheme, text(case, "scheme"), "{input:?}");
                assert_eq!(url.host, text(case, "host"), "{input:?}");
                assert_eq!(url.port, case.get("port").unwrap().as_i64(), "{input:?}");
                assert_eq!(url.raw_path(), text(case, "raw_path"), "{input:?}");
                assert_eq!(url.netloc(), text(case, "netloc"), "{input:?}");
                assert_eq!(url.username(), text(case, "username"), "{input:?}");
                assert_eq!(url.password(), text(case, "password"), "{input:?}");
                assert_eq!(
                    Some(url.is_relative()),
                    case.get("is_relative").unwrap().as_bool(),
                    "{input:?}"
                );
            }
        }
    }
}

#[test]
fn redirect_resolution_matches_httpx() {
    let fixture = fixture();
    let cases = fixture.get("redirect").unwrap().as_list().unwrap();
    for case in cases {
        let base = HttpxUrl::parse(text(case, "base")).unwrap();
        let location = text(case, "location");
        match redirect_url(&base, location) {
            Err(err) => {
                let request_error = case
                    .get("request_error")
                    .and_then(PyJson::as_bool)
                    .unwrap_or_else(|| panic!("{location:?} must resolve: {err:?}"));
                assert_eq!(
                    err.kind == HttpErrorKind::Request,
                    request_error,
                    "{location:?}"
                );
            }
            Ok(url) => {
                assert!(
                    case.get("error").is_none(),
                    "{location:?} must fail: {url:?}"
                );
                assert_eq!(url.to_string(), text(case, "str"), "{location:?}");
                assert_eq!(url.raw_path(), text(case, "raw_path"), "{location:?}");
            }
        }
    }
}

#[test]
fn urljoin_matches_rfc3986_examples() {
    let base = "http://a/b/c/d;p?q";
    for (reference, expected) in [
        ("g", "http://a/b/c/g"),
        ("./g", "http://a/b/c/g"),
        ("g/", "http://a/b/c/g/"),
        ("/g", "http://a/g"),
        ("?y", "http://a/b/c/d;p?y"),
        ("g?y", "http://a/b/c/g?y"),
        ("#s", "http://a/b/c/d;p?q#s"),
        (";x", "http://a/b/c/;x"),
        ("", "http://a/b/c/d;p?q"),
        (".", "http://a/b/c/"),
        ("..", "http://a/b/"),
        ("../g", "http://a/b/g"),
        ("../..", "http://a/"),
        ("../../../g", "http://a/g"),
    ] {
        assert_eq!(urljoin(base, reference), expected, "{reference}");
    }
}
