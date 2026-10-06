"""DnsHealthCheckForm stores canonical addresses of the queried family."""

import pytest

from nyxboard.forms import DnsHealthCheckForm
from nyxboard.models import Service
from nyxmon.domain import CheckType


@pytest.fixture
def service(db):
    return Service.objects.create(name="Test Service")


def _form(service, expected_ips: str, query_type: str) -> DnsHealthCheckForm:
    return DnsHealthCheckForm(
        data={
            "name": "DNS",
            "service": service.id,
            "check_type": CheckType.DNS,
            "url": "example.com",
            "check_interval": 300,
            "disabled": False,
            "expected_ips": expected_ips,
            "query_type": query_type,
            "timeout": 5.0,
        }
    )


def test_aaaa_expectation_is_stored_compressed(service):
    form = _form(service, "2A01:04F8:0000::0001\n2001:DB8::A", "AAAA")

    assert form.is_valid(), form.errors
    check = form.save()

    assert check.data["expected_ips"] == ["2a01:4f8::1", "2001:db8::a"]


def test_ipv4_expectation_on_aaaa_check_is_rejected(service):
    form = _form(service, "2001:db8::1\n192.0.2.1", "AAAA")

    assert not form.is_valid()
    assert "IPv4" in str(form.errors["expected_ips"])


def test_ipv6_expectation_on_a_check_is_rejected(service):
    form = _form(service, "2001:db8::1", "A")

    assert not form.is_valid()
    assert "IPv6" in str(form.errors["expected_ips"])


def test_ipv4_expectation_on_a_check_is_accepted(service):
    form = _form(service, "192.0.2.1", "A")

    assert form.is_valid(), form.errors
    assert form.cleaned_data["expected_ips"] == ["192.0.2.1"]
