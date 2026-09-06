"""Dashboard rendering of the site connectivity incident and held alerts.

The banner and the held indicator are rendered from rows the worker writes, so
every test here builds those rows directly. A payload the dashboard cannot read
must never raise: the malformed cases assert a normal page without a banner.

The dashboard cannot see the worker's mode and cannot re-run the hold decision,
so both surfaces must report an observation rather than an outcome: the banner
states the measured site condition and makes holding conditional on ``enforce``
mode, and the check detail page reads "held" off the newest stored sample
instead of inferring it from ``held_since``.
"""

from datetime import datetime, timedelta, timezone

import pytest
from django.db import connection
from django.urls import reverse

from nyxmon.domain import ResultStatus

from nyxboard.models import CheckNotificationState, CollectorIncident, Result
from nyxboard.views import SITE_INCIDENT_KEY, SITE_OBSERVATION_STALE_SECONDS

#: ``observed_at`` of the payloads built below.
OBSERVED_AT = 1757125000


def _incident(payload):
    return CollectorIncident.objects.create(
        incident_key=SITE_INCIDENT_KEY,
        opened_at=1757123456,
        last_alert_at=0,
        alert_count=0,
        payload=payload,
    )


def _freeze(monkeypatch, now):
    """Pin the clock the banner compares ``observed_at`` against."""
    monkeypatch.setattr("nyxboard.views.time", lambda: float(now))


def _stamp(result, *, seconds_ago):
    """Give ``result`` an explicit ``created_at`` so ordering is deterministic."""
    moment = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)
    Result.objects.filter(pk=result.pk).update(created_at=moment)
    return result


def _stamp_at(result, moment):
    """Give ``result`` an exact ``created_at``, shared with other results.

    The worker writes second-resolution timestamps, so two samples of the same
    check can carry the very same ``created_at``; this reproduces that tie.
    """
    Result.objects.filter(pk=result.pk).update(created_at=moment)
    return result


def _active_payload(**overrides):
    payload = {
        "incident_type": "site_connectivity",
        "version": 1,
        "phase": "active",
        "incident_id": 1757123456,
        "observed_at": OBSERVED_AT,
        "paths": {
            "dns": {
                "state": "up",
                "down_since": 0,
                "recovered_at": 0,
                "release_at": 0,
                "last_release_at": 0,
            },
            "ipv4": {
                "state": "down",
                "down_since": 1757123456,
                "recovered_at": 0,
                "release_at": 0,
                "last_release_at": 0,
            },
            "ipv6": {
                "state": "recovering",
                "down_since": 1757123500,
                "recovered_at": 1757124900,
                "release_at": 1757125800,
                "last_release_at": 0,
            },
        },
        "ongoing": {},
        "summaries": [],
        "recheck": {},
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
class TestDashboardBanner:
    def test_no_banner_without_an_incident_row(self, client):
        response = client.get(reverse("nyxboard:dashboard"))

        assert response.status_code == 200
        assert response.context["site_connectivity"] is None
        assert b"site-connectivity-banner" not in response.content

    def test_no_banner_while_the_row_is_idle(self, client):
        _incident(_active_payload(phase="idle", summaries=[]))

        response = client.get(reverse("nyxboard:dashboard"))

        assert response.status_code == 200
        assert response.context["site_connectivity"] is None
        assert b"site-connectivity-banner" not in response.content

    def test_banner_names_the_affected_paths_of_an_active_outage(
        self, client, monkeypatch
    ):
        _freeze(monkeypatch, OBSERVED_AT + 60)
        _incident(_active_payload())

        response = client.get(reverse("nyxboard:dashboard"))
        content = response.content.decode()

        assert response.status_code == 200
        banner = response.context["site_connectivity"]
        assert banner["active"] is True
        assert banner["summary_pending"] is False
        assert [path["name"] for path in banner["paths"]] == ["ipv4", "ipv6"]
        assert banner["paths"][0]["down_since"] is not None
        assert "site-connectivity-banner" in content
        assert "Site connectivity outage observed" in content
        # ``dns`` is up and must not be named as affected.
        assert "ipv4 (down" in content
        assert "ipv6 (recovering" in content
        assert "summary" not in content.lower()

    def test_banner_makes_holding_conditional_on_enforce_mode(
        self, client, monkeypatch
    ):
        # The dashboard cannot know the worker's mode, so it must never claim
        # that alerts are being held.
        _freeze(monkeypatch, OBSERVED_AT + 60)
        _incident(_active_payload())

        response = client.get(reverse("nyxboard:dashboard"))
        content = response.content.decode()

        assert "held only while the worker runs in" in content
        assert "<code>enforce</code> mode" in content
        # The old, unconditional claim must be gone.
        assert "<code>site_dependency</code> are held." not in content

    def test_banner_reports_a_fresh_observation_time(self, client, monkeypatch):
        _freeze(monkeypatch, OBSERVED_AT + SITE_OBSERVATION_STALE_SECONDS)
        _incident(_active_payload())

        response = client.get(reverse("nyxboard:dashboard"))
        content = response.content.decode()

        banner = response.context["site_connectivity"]
        assert banner["stale"] is False
        assert banner["observed_at"] is not None
        assert f"Last observed {banner['observed_at']}." in content
        assert "the observer may be stopped" not in content

    def test_banner_warns_when_the_observation_is_old(self, client, monkeypatch):
        _freeze(monkeypatch, OBSERVED_AT + SITE_OBSERVATION_STALE_SECONDS + 1)
        _incident(_active_payload())

        response = client.get(reverse("nyxboard:dashboard"))
        content = response.content.decode()

        banner = response.context["site_connectivity"]
        assert banner["stale"] is True
        assert f"Last observed {banner['observed_at']}" in content
        assert "the observer may be stopped" in content

    @pytest.mark.parametrize("observed_at", [0, -1, "recently", None, True])
    def test_an_unusable_observation_time_counts_as_stale(
        self, client, monkeypatch, observed_at
    ):
        _freeze(monkeypatch, OBSERVED_AT)
        _incident(_active_payload(observed_at=observed_at))

        response = client.get(reverse("nyxboard:dashboard"))
        content = response.content.decode()

        banner = response.context["site_connectivity"]
        assert banner["stale"] is True
        assert banner["observed_at"] is None
        assert "Last observed at an unknown time" in content
        assert "the observer may be stopped" in content

    def test_banner_reports_a_pending_summary_while_idle(self, client, monkeypatch):
        _freeze(monkeypatch, OBSERVED_AT + 60)
        _incident(
            _active_payload(
                phase="idle",
                summaries=[{"incident_id": 1757123456, "attempt_at": 0}],
                paths={},
            )
        )

        response = client.get(reverse("nyxboard:dashboard"))
        content = response.content.decode()

        banner = response.context["site_connectivity"]
        assert banner["active"] is False
        assert banner["summary_pending"] is True
        assert banner["paths"] == []
        assert "Site connectivity recovered" in content
        assert "recovery summary is still pending delivery" in content
        # Nothing is held once every path is up again.
        assert "held only while" not in content

    def test_recovered_banner_still_names_a_path_that_is_not_up(
        self, client, monkeypatch
    ):
        # The observer only goes idle once nothing blocks, but the banner
        # reports what the payload says rather than what it should say.
        _freeze(monkeypatch, OBSERVED_AT + 60)
        _incident(
            _active_payload(
                phase="idle",
                summaries=[{"incident_id": 1757123456, "attempt_at": 0}],
            )
        )

        response = client.get(reverse("nyxboard:dashboard"))
        content = response.content.decode()

        assert "Site connectivity recovered" in content
        assert "Still reported as not up" in content
        assert "ipv4 (down" in content
        assert "Every observed path is up again" not in content

    @pytest.mark.parametrize(
        "payload",
        [
            "not a dict",
            [],
            42,
            {"phase": "active", "paths": "broken"},
            {"phase": "active", "paths": {"ipv4": "broken"}},
            {"phase": "active", "paths": {"ipv4": {"state": "down", "down_since": {}}}},
            {"phase": "active", "paths": {"ipv4": {"state": "down"}}, "summaries": 7},
            {"phase": 42, "summaries": {}},
            {"phase": "active", "observed_at": float("nan")},
            {"phase": "active", "observed_at": 10**20},
        ],
    )
    def test_malformed_payload_never_raises(self, client, payload):
        _incident(payload)

        response = client.get(reverse("nyxboard:dashboard"))

        assert response.status_code == 200

    def test_json_null_payload_never_raises(self, client):
        # ``payload`` is NOT NULL, so a JSON ``null`` can only arrive through a
        # raw write; the reader still has to survive it.
        _incident({})
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE collector_incident SET payload = 'null' "
                "WHERE incident_key = %s",
                [SITE_INCIDENT_KEY],
            )

        response = client.get(reverse("nyxboard:dashboard"))

        assert response.status_code == 200
        assert response.context["site_connectivity"] is None

    def test_malformed_payload_shows_no_banner(self, client):
        _incident({"phase": "idle", "summaries": "broken"})

        response = client.get(reverse("nyxboard:dashboard"))

        assert response.context["site_connectivity"] is None
        assert b"site-connectivity-banner" not in response.content

    def test_active_payload_with_unusable_timestamps_still_renders(self, client):
        _incident(
            {
                "phase": "active",
                "incident_id": "not an int",
                "paths": {
                    "ipv4": {"state": "down", "down_since": True},
                    "ipv6": {"state": "down", "down_since": -5},
                },
            }
        )

        response = client.get(reverse("nyxboard:dashboard"))

        banner = response.context["site_connectivity"]
        assert response.status_code == 200
        assert banner["incident_id"] is None
        assert [path["down_since"] for path in banner["paths"]] == [None, None]
        assert banner["stale"] is True
        assert b"site-connectivity-banner" in response.content


def _held_data(held, **extra):
    return {
        "error_type": "timeout",
        "site_connectivity": {
            "held": held,
            "reason": "dependency_down",
            "paths": ["ipv4"],
            **extra,
        },
    }


@pytest.mark.django_db
class TestCheckDetailHeldIndicator:
    def test_no_indicator_without_notification_state(
        self, client, service_factory, healthcheck_factory
    ):
        check = healthcheck_factory(service_factory())

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )

        assert response.status_code == 200
        assert response.context["held_since"] is None
        assert response.context["alert_held"] is False
        assert b"site-connectivity-held" not in response.content
        assert b"site-connectivity-hold-budget" not in response.content

    def test_no_indicator_while_the_check_is_not_held(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        check = healthcheck_factory(service_factory())
        CheckNotificationState.objects.create(health_check=check, held_since=0)
        result_factory(check, status=ResultStatus.ERROR, data={"error_type": "timeout"})

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )

        assert response.context["held_since"] is None
        assert response.context["alert_held"] is False
        assert b"site-connectivity-held" not in response.content
        assert b"site-connectivity-hold-budget" not in response.content

    def test_indicator_shown_while_the_latest_sample_was_held(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        check = healthcheck_factory(service_factory())
        CheckNotificationState.objects.create(
            health_check=check, failure_count=3, held_since=1757123456
        )
        _stamp(
            result_factory(check, status=ResultStatus.ERROR, data=_held_data(True)),
            seconds_ago=10,
        )

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )
        content = response.content.decode()

        assert response.context["alert_held"] is True
        assert response.context["held_since"] is not None
        assert "site-connectivity-held" in content
        assert "The latest sample was held by site connectivity" in content
        assert "hold budget started" in content
        # The check does not necessarily alert on its first fresh sample.
        assert "according to its own threshold and reminder policy" in content
        assert "first fresh sample" not in content

    def test_exhausted_hold_reports_the_budget_and_normal_policy(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        # ``held_since`` deliberately stays non-zero after exhaustion, so it
        # alone must not be read as "held".
        check = healthcheck_factory(service_factory())
        CheckNotificationState.objects.create(
            health_check=check, failure_count=9, held_since=1757123456
        )
        _stamp(
            result_factory(
                check,
                status=ResultStatus.ERROR,
                data=_held_data(False, exhausted=True),
            ),
            seconds_ago=10,
        )

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )
        content = response.content.decode()

        assert response.context["alert_held"] is False
        assert "site-connectivity-held" not in content
        assert "site-connectivity-hold-budget" in content
        assert "Hold budget started" in content
        assert "alerts follow normal policy" in content

    def test_observed_only_sample_reports_the_budget(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        # ``observe`` mode records the judgement with ``held: false``.
        check = healthcheck_factory(service_factory())
        CheckNotificationState.objects.create(
            health_check=check, failure_count=2, held_since=1757123456
        )
        _stamp(
            result_factory(check, status=ResultStatus.ERROR, data=_held_data(False)),
            seconds_ago=10,
        )

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )
        content = response.content.decode()

        assert response.context["alert_held"] is False
        assert "site-connectivity-held" not in content
        assert "Hold budget started" in content

    def test_a_later_unheld_sample_supersedes_an_earlier_held_one(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        check = healthcheck_factory(service_factory())
        CheckNotificationState.objects.create(
            health_check=check, failure_count=4, held_since=1757123456
        )
        _stamp(
            result_factory(check, status=ResultStatus.ERROR, data=_held_data(True)),
            seconds_ago=600,
        )
        _stamp(
            result_factory(
                check, status=ResultStatus.ERROR, data={"error_type": "timeout"}
            ),
            seconds_ago=10,
        )

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )
        content = response.content.decode()

        assert response.context["alert_held"] is False
        assert "site-connectivity-held" not in content
        assert "Hold budget started" in content

    def test_same_second_results_are_ordered_by_insertion(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        # Second-resolution timestamps make ties routine: the held error and
        # the OK sample that supersedes it can share one ``created_at``, and
        # only the row id then says which one the worker wrote last.
        check = healthcheck_factory(service_factory())
        CheckNotificationState.objects.create(
            health_check=check, failure_count=4, held_since=1757123456
        )
        moment = datetime.now(tz=timezone.utc) - timedelta(seconds=10)
        held = _stamp_at(
            result_factory(check, status=ResultStatus.ERROR, data=_held_data(True)),
            moment,
        )
        recovered = _stamp_at(
            result_factory(check, status=ResultStatus.OK, data={"response_time": 0.5}),
            moment,
        )
        assert recovered.pk > held.pk

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )
        content = response.content.decode()

        assert [result.pk for result in response.context["results"]] == [
            recovered.pk,
            held.pk,
        ]
        assert response.context["alert_held"] is False
        assert "site-connectivity-held" not in content
        assert "Hold budget started" in content

    def test_hold_marker_without_any_result_reports_the_budget(
        self, client, service_factory, healthcheck_factory
    ):
        check = healthcheck_factory(service_factory())
        CheckNotificationState.objects.create(
            health_check=check, failure_count=1, held_since=1757123456
        )

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )
        content = response.content.decode()

        assert response.context["alert_held"] is False
        assert "site-connectivity-held" not in content
        assert "Hold budget started" in content

    def test_results_carry_a_held_or_observed_badge(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        check = healthcheck_factory(service_factory())
        result_factory(check, status=ResultStatus.OK, data={"response_time": 0.5})
        result_factory(check, status=ResultStatus.ERROR, data=_held_data(False))
        result_factory(check, status=ResultStatus.ERROR, data=_held_data(True))

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )
        content = response.content.decode()
        badges = [
            result.site_connectivity_badge for result in response.context["results"]
        ]

        assert response.status_code == 200
        assert sorted(badge for badge in badges if badge) == ["held", "observed"]
        assert badges.count(None) == 1
        assert "site-connectivity-badge-held" in content
        assert "site-connectivity-badge-observed" in content

    def test_malformed_result_metadata_carries_no_badge(
        self, client, service_factory, healthcheck_factory, result_factory
    ):
        check = healthcheck_factory(service_factory())
        result_factory(
            check, status=ResultStatus.ERROR, data={"site_connectivity": "broken"}
        )

        response = client.get(
            reverse("nyxboard:healthcheck_detail", kwargs={"check_id": check.id})
        )

        assert response.status_code == 200
        assert response.context["results"][0].site_connectivity_badge is None
        assert response.context["alert_held"] is False
        assert b"site-connectivity-badge" not in response.content
