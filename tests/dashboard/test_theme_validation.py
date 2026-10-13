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
