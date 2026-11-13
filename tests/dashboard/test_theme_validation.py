import pytest
from django.contrib.sessions.models import Session
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "null",
        "1",
        '"dark"',
        "{}",
        '{"theme":"other"}',
        '{"theme":[]}',
        '{"theme":null}',
        "{bad",
    ],
)
def test_invalid_theme_does_not_create_session(client, body):
    response = client.post(
        reverse("nyxboard:set_theme"), body, content_type="application/json"
    )
    assert response.status_code == 400
    assert not Session.objects.exists()
    assert "sessionid" not in response.cookies


def test_default_theme_does_not_create_session(client):
    response = client.post(
        reverse("nyxboard:set_theme"),
        {"theme": "light"},
        content_type="application/json",
    )
    assert response.status_code == 200
    assert not Session.objects.exists()
    assert "sessionid" not in response.cookies


def test_dark_is_stored_and_light_removes_only_preference(client):
    url = reverse("nyxboard:set_theme")
    assert (
        client.post(url, {"theme": "dark"}, content_type="application/json").status_code
        == 200
    )
    assert client.session["theme"] == "dark"
    session = client.session
    session["other"] = "keep"
    session.save()
    assert (
        client.post(
            url, {"theme": "light"}, content_type="application/json"
        ).status_code
        == 200
    )
    assert "theme" not in client.session
    assert client.session["other"] == "keep"


def test_non_utf8_theme_returns_400(client):
    assert (
        client.post(
            reverse("nyxboard:set_theme"), b"\xff", content_type="application/json"
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 3000 + b"]" * 3000,
        b'{"theme":' + b"1" * 5000 + b"}",
        b'{"theme":"dark","theme":"light"}',
        b'{"theme":"dark","other":1,"other":2}',
        b"\xff",
        b" " * 4097 + b'{"theme":"dark"}',
    ],
    ids=[
        "deep",
        "big-integer",
        "duplicate-theme",
        "duplicate-member",
        "utf8",
        "oversized",
    ],
)
def test_json_resource_errors_leave_existing_session_unchanged(client, body):
    session = client.session
    session.update({"theme": "dark", "synthetic": "keep"})
    session.save()
    response = client.post(
        reverse("nyxboard:set_theme"), body, content_type="application/json"
    )
    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": "Invalid JSON"}
    assert dict(client.session) == {"theme": "dark", "synthetic": "keep"}


def test_in_budget_integer_decoder_resource_refusal_is_fixed_400(client):
    import sys

    session = client.session
    session.update({"theme": "dark", "synthetic": "keep"})
    session.save()
    integer_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        body = b'{"theme":"light","number":' + b"1" * 700 + b"}"
        response = client.post(
            reverse("nyxboard:set_theme"), body, content_type="application/json"
        )
    finally:
        sys.set_int_max_str_digits(integer_limit)
    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": "Invalid JSON"}
    assert dict(client.session) == {"theme": "dark", "synthetic": "keep"}
