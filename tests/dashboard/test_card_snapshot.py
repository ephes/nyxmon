"""Real SQLite WAL completions during dashboard evidence reads."""

import sqlite3
from itertools import cycle

import pytest
from django.db import connection, connections, router
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from nyxboard.forms import HealthCheckForm
from nyxboard.models import CheckNotificationState, HealthCheck, Result, Service

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def card_db(
    tmp_path,
    monkeypatch,
    django_db_blocker,
    service_factory,
    healthcheck_factory,
    result_factory,
):
    check = healthcheck_factory(
        service_factory(),
        status="processing",
        processing_started_at=777,
        next_check_time=9000,
    )
    sample = result_factory(check, status="ok")
    path = tmp_path / "card.sqlite3"
    with sqlite3.connect(path) as copied:
        connection.connection.backup(copied)
        copied.execute("PRAGMA journal_mode=WAL")
    alias = "card_snapshot"
    connections.databases[alias] = {**connection.settings_dict, "NAME": str(path)}
    for name in ["db_for_read", "db_for_write"]:
        original = getattr(router, name)
        monkeypatch.setattr(
            router,
            name,
            lambda model, _original=original, **hints: alias
            if model in (HealthCheck, Result, Service)
            else _original(model, **hints),
        )
    with django_db_blocker.unblock():
        yield check, sample, connections[alias], path
    connections[alias].close()
    del connections[alias]
    del connections.databases[alias]


def test_card_pins_check_and_results_when_read_routing_varies(
    client, card_db, monkeypatch
):
    check, sample, conn, _ = card_db
    HealthCheck.objects.using("default").filter(pk=check.pk).update(name="Other check")
    Result.objects.using("default").filter(pk=sample.pk).update(status="error")

    aliases = cycle([conn.alias, "default"])
    original_read = router.db_for_read

    def route_read(model, **hints):
        if model in (HealthCheck, Result):
            return next(aliases)
        return original_read(model, **hints)

    monkeypatch.setattr(router, "db_for_read", route_read)
    response = client.get(
        reverse("nyxboard:healthcheck_update_status", args=[check.pk])
    )

    assert response.status_code == 200
    assert response.context["check"].name == check.name
    assert response.context["last_result"].status == sample.status
    assert response.context["check"]._state.db == conn.alias
    assert response.context["last_result"]._state.db == conn.alias


@pytest.mark.parametrize(
    "path",
    ["healthcheck_update_status", "healthcheck_trigger", "healthcheck_toggle_disabled"],
)
@pytest.mark.parametrize("partial", ["healthcheck-card.html", "healthcheck.html"])
def test_card_uses_one_snapshot_without_render_time_queries(
    client, card_db, path, partial, monkeypatch
):
    from nyxboard import views

    check, sample, conn, db_path = card_db
    token = HealthCheckForm(instance=check)["definition_revision"].value()
    committed = []

    def complete(execute, sql, params, many, context):
        if (
            '"check_result"' in sql
            and sql.lstrip().upper().startswith("SELECT")
            and not committed
        ):
            # A second connection atomically publishes a completion after the
            # view loaded the check, before it loads the latest result.
            with sqlite3.connect(db_path, timeout=1) as writer:
                writer.execute(
                    "UPDATE health_check SET status='idle', processing_started_at=0, next_check_time=9999999999 WHERE id=?",
                    [check.pk],
                )
                writer.execute(
                    "INSERT INTO check_result (health_check_id, status, data, created_at) VALUES (?, 'error', '{}', '2099-01-01 00:00:00')",
                    [check.pk],
                )
            committed.append(True)
        return execute(sql, params, many, context)

    original_render = views.render

    def render(*args, **kwargs):
        assert not conn.in_atomic_block, "rendering must not retain the DB transaction"
        with CaptureQueriesContext(conn) as queries:
            response = original_render(*args, **kwargs)
        assert len(queries) == 0
        return response

    monkeypatch.setattr(views, "render", render)
    method = client.get if path == "healthcheck_update_status" else client.post
    with conn.execute_wrapper(complete), CaptureQueriesContext(conn) as queries:
        response = method(
            reverse(f"nyxboard:{path}", args=[check.pk]),
            {"definition_revision": token},
            headers={"HX-Request-URL": partial},
        )
    assert committed
    assert response.status_code == 200
    current = response.context["check"]
    assert current.status == "processing"
    assert current.processing_started_at == 777
    assert response.context["last_result"].pk == sample.pk
    assert [r.pk for r in current.recent_results] == [sample.pk]
    selects = [q for q in queries if q["sql"].lstrip().startswith("SELECT")]
    assert len(selects) <= 4
    # A subsequent poll sees the complete new generation.
    fresh = client.get(reverse("nyxboard:healthcheck_update_status", args=[check.pk]))
    assert fresh.context["check"].status == "idle"
    assert fresh.context["check"].next_check_time == 9999999999
    assert fresh.context["last_result"].status == "error"
    assert fresh.context["check_mode"] == "normal"


def test_detail_uses_one_snapshot_for_history_hold_and_status(
    client, card_db, monkeypatch
):
    from nyxboard import views

    check, sample, conn, db_path = card_db
    CheckNotificationState.objects.using(conn.alias).create(
        health_check_id=check.pk, held_since=1757123456
    )
    read_route = router.db_for_read
    monkeypatch.setattr(
        router,
        "db_for_read",
        lambda model, **hints: conn.alias
        if model is CheckNotificationState
        else read_route(model, **hints),
    )
    samples = [
        Result.objects.using(conn.alias).create(health_check_id=check.pk, status="ok")
        for _ in range(9)
    ]
    held = Result.objects.using(conn.alias).create(
        health_check_id=check.pk,
        status="error",
        data={"site_connectivity": {"held": True}},
    )
    Result.objects.using(conn.alias).filter(health_check_id=check.pk).update(
        created_at=sample.created_at
    )
    recovery_id = None

    def complete(execute, sql, params, many, context):
        nonlocal recovery_id
        if '"check_notification_state"' in sql and recovery_id is None:
            # Publish a recovery between the history read and the hold read.
            with sqlite3.connect(db_path, timeout=1) as writer:
                recovery_id = writer.execute(
                    "INSERT INTO check_result (health_check_id, status, data, created_at) "
                    "SELECT health_check_id, 'ok', '{}', created_at FROM check_result WHERE id=?",
                    [held.pk],
                ).lastrowid
                writer.execute(
                    "UPDATE check_notification_state SET held_since=0 WHERE check_id=?",
                    [check.pk],
                )
        return execute(sql, params, many, context)

    render_queries = []
    original_render = views.render

    def render(*args, **kwargs):
        assert not conn.in_atomic_block
        with CaptureQueriesContext(conn) as queries:
            response = original_render(*args, **kwargs)
        render_queries.extend(queries)
        return response

    monkeypatch.setattr(views, "render", render)
    url = reverse("nyxboard:healthcheck_detail", args=[check.pk])
    with conn.execute_wrapper(complete), CaptureQueriesContext(conn) as queries:
        response = client.get(url)

    assert recovery_id is not None
    assert response.status_code == 200
    assert response.context["held_since"] == "Sep 6, 01:50 UTC"
    assert response.context["alert_held"] is True
    assert b'status-failed">Failed</span>' in response.content
    assert [row.pk for row in response.context["results"]] == [
        held.pk,
        *(row.pk for row in reversed(samples)),
    ]
    assert not render_queries
    assert sum('"check_result"' in query["sql"] for query in queries) == 1

    fresh = client.get(url)
    assert fresh.context["results"][0].pk == recovery_id
    assert fresh.context["held_since"] is None
    assert fresh.context["alert_held"] is False
    assert b'status-recovering">Recovering</span>' in fresh.content
