"""Password admission across independent connections to a synthetic SQLite DB."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.db import connection, connections, router
from django.test import RequestFactory

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def admission_db(settings, tmp_path, monkeypatch, django_db_blocker, django_user_model):
    cache.clear()
    settings.NYXBOARD_LOGIN_FAILURE_LIMIT = 2
    settings.NYXBOARD_LOGIN_ADDRESS_FAILURE_LIMIT = 5
    settings.NYXBOARD_LOGIN_FAILURE_WINDOW_SECONDS = 900
    user = django_user_model.objects.create_user("operator", password="synthetic")
    path = tmp_path / "admission.sqlite3"
    with sqlite3.connect(path) as copied:
        connection.connection.backup(copied)
    alias = "login_admission"
    connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}
    from nyxboard.models import LoginThrottleBucket

    original = router.db_for_write
    monkeypatch.setattr(
        router,
        "db_for_write",
        lambda model, **hints: alias
        if model is LoginThrottleBucket
        else original(model, **hints),
    )
    with django_db_blocker.unblock():
        yield user, LoginThrottleBucket.objects.using(alias)
    connections[alias].close()
    del connections[alias]
    del connections.databases[alias]


def login():
    try:
        return authenticate(
            RequestFactory().post("/"), username="operator", password="wrong"
        )
    finally:
        connections.close_all()


def test_last_slot_is_reserved_before_password_work(admission_db, monkeypatch):
    user, buckets = admission_db
    assert login() is None  # one completed failure
    entered, release = threading.Event(), threading.Event()
    calls = []

    def password(self, raw):
        calls.append(raw)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return False

    monkeypatch.setattr(type(user), "check_password", password)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(login)
        try:
            assert entered.wait(5)
            assert pool.submit(login).result(5) is None
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(5) is None
    assert list(buckets.values_list("failures", "pending")) == [(2, 0), (2, 0)]


def test_parallel_failures_are_not_lost(admission_db, monkeypatch):
    user, buckets = admission_db
    barrier = threading.Barrier(2)

    def password(self, raw):
        barrier.wait(5)
        return False

    monkeypatch.setattr(type(user), "check_password", password)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(login) for _ in range(2)]
        assert [f.result(5) for f in futures] == [None, None]
    assert list(buckets.values_list("failures", "pending")) == [(2, 0), (2, 0)]
    monkeypatch.setattr(
        type(user), "check_password", lambda *args: pytest.fail("limit bypassed")
    )
    assert login() is None


def test_password_cancellation_releases_reservation(admission_db, monkeypatch):
    user, buckets = admission_db

    def cancelled(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(type(user), "check_password", cancelled)
    with pytest.raises(KeyboardInterrupt):
        login()
    assert list(buckets.values_list("failures", "pending")) == [(0, 0), (0, 0)]
    monkeypatch.setattr(type(user), "check_password", lambda *args: True)
    assert login() == user


def test_old_completion_cannot_debit_replacement_window(admission_db, monkeypatch):
    from nyxboard import login_throttle

    user, buckets = admission_db
    now = [1000.0]
    monkeypatch.setattr(login_throttle.time, "time", lambda: now[0])
    request = RequestFactory().post("/")
    old = login_throttle._reserve(request, user.username)
    now[0] += 901
    current = login_throttle._reserve(request, user.username)
    login_throttle._settle(
        old, login_throttle._keys(request, user.username)[0], success=False, failed=True
    )
    assert list(buckets.values_list("failures", "pending")) == [(0, 1), (0, 1)]
    login_throttle._settle(
        current,
        login_throttle._keys(request, user.username)[0],
        success=False,
        failed=True,
    )
    assert list(buckets.values_list("failures", "pending")) == [(1, 0), (1, 0)]


def test_failure_budget_is_shared_by_separate_processes(admission_db):
    import os
    from pathlib import Path
    import subprocess
    import sys

    _, buckets = admission_db
    path = connections[buckets.db].settings_dict["NAME"]
    script = """
import sys
import django
from django.conf import settings
settings.DATABASES["default"]["NAME"] = sys.argv[1]
settings.NYXBOARD_LOGIN_FAILURE_LIMIT = 2
settings.NYXBOARD_LOGIN_ADDRESS_FAILURE_LIMIT = 5
django.setup()
from django.contrib.auth import authenticate, get_user_model
from django.test import RequestFactory
calls = []
get_user_model().check_password = lambda self, raw: calls.append(raw) or False
assert authenticate(RequestFactory().post("/"), username="operator", password="wrong") is None
print(len(calls))
"""
    root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "PYTHONPATH": os.pathsep.join(
            [str(root / "src" / "django"), str(root / "src")]
        ),
    }
    counts = []
    for _ in range(3):
        result = subprocess.run(
            [sys.executable, "-c", script, path],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        counts.append(int(result.stdout.strip()))
    assert counts == [1, 1, 0]
