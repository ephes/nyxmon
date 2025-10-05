"""Tests for Django forms."""

import pytest

from nyxboard.forms import (
    HttpHealthCheckForm,
    DnsHealthCheckForm,
    GenericHealthCheckForm,
)
from nyxboard.models import Service, HealthCheck
from nyxmon.domain import CheckType


@pytest.fixture
def service(db):
    """Create a test service."""
    return Service.objects.create(name="Test Service")


class TestHttpHealthCheckForm:
    """Tests for HttpHealthCheckForm."""

    def test_check_type_auto_set_to_http(self, service):
        """Test that check_type is automatically set to HTTP."""
        form = HttpHealthCheckForm(
            data={
                "name": "Test HTTP Check",
                "service": service.id,
                "check_type": CheckType.HTTP,  # Hidden field, but needs to be in POST data
                "url": "https://example.com",
                "check_interval": 300,
                "disabled": False,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.check_type == CheckType.HTTP

    def test_check_type_hidden_in_form(self, service):
        """Test that check_type field is hidden."""
        form = HttpHealthCheckForm()
        assert form.fields["check_type"].widget.__class__.__name__ == "HiddenInput"

    def test_url_field_label(self, service):
        """Test that url field label is 'URL'."""
        form = HttpHealthCheckForm()
        assert form.fields["url"].label == "URL"

    def test_url_validator_appended(self, service):
        """Test that URLValidator is appended to url field validators."""
        form = HttpHealthCheckForm()
        validator_classes = [
            v.__class__.__name__ for v in form.fields["url"].validators
        ]
        assert "URLValidator" in validator_classes

    def test_valid_url_passes(self, service):
        """Test that valid URLs pass validation."""
        form = HttpHealthCheckForm(
            data={
                "name": "Test HTTP Check",
                "service": service.id,
                "check_type": CheckType.HTTP,
                "url": "https://example.com/api/health",
                "check_interval": 300,
                "disabled": False,
            }
        )
        assert form.is_valid(), form.errors

    def test_invalid_url_fails(self, service):
        """Test that invalid URLs fail validation."""
        form = HttpHealthCheckForm(
            data={
                "name": "Test HTTP Check",
                "service": service.id,
                "check_type": CheckType.HTTP,
                "url": "not-a-valid-url",
                "check_interval": 300,
                "disabled": False,
            }
        )
        assert not form.is_valid()
        assert "url" in form.errors

    def test_data_field_set_to_empty_dict(self, service):
        """Test that data field is set to empty dict."""
        form = HttpHealthCheckForm(
            data={
                "name": "Test HTTP Check",
                "service": service.id,
                "check_type": CheckType.HTTP,
                "url": "https://example.com",
                "check_interval": 300,
                "disabled": False,
            }
        )
        assert form.is_valid()
        instance = form.save()
        assert instance.data == {}

    def test_check_type_tampering_prevention(self, service):
        """Test that check_type is explicitly set in save() to prevent tampering."""
        # Try to set check_type to DNS in POST data
        form = HttpHealthCheckForm(
            data={
                "name": "Test HTTP Check",
                "service": service.id,
                "check_type": CheckType.DNS,  # Attempt tampering
                "url": "https://example.com",
                "check_interval": 300,
                "disabled": False,
            }
        )
        assert form.is_valid()
        instance = form.save()
        # Should still be HTTP, not DNS
        assert instance.check_type == CheckType.HTTP

    def test_json_http_check_type_preserved_on_create(self, service):
        """Test that JSON-HTTP check type is preserved when creating via initial data."""
        form = HttpHealthCheckForm(
            data={
                "name": "Test JSON-HTTP Check",
                "service": service.id,
                "check_type": CheckType.JSON_HTTP,
                "url": "https://api.example.com/health",
                "check_interval": 300,
                "disabled": False,
            },
            initial={"check_type": CheckType.JSON_HTTP},
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.check_type == CheckType.JSON_HTTP

    def test_json_http_check_type_preserved_on_edit(self, service):
        """Test that JSON-HTTP check type is preserved when editing."""
        # Create a JSON-HTTP check
        health_check = HealthCheck.objects.create(
            name="Existing JSON-HTTP Check",
            service=service,
            check_type=CheckType.JSON_HTTP,
            url="https://api.example.com/health",
            check_interval=300,
        )

        # Edit it via form
        form = HttpHealthCheckForm(
            data={
                "name": "Updated JSON-HTTP Check",
                "service": service.id,
                "check_type": CheckType.HTTP,  # Try to tamper
                "url": "https://api.example.com/health/v2",
                "check_interval": 300,
                "disabled": False,
            },
            instance=health_check,
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        # Should still be JSON-HTTP, not HTTP
        assert instance.check_type == CheckType.JSON_HTTP


class TestDnsHealthCheckForm:
    """Tests for DnsHealthCheckForm."""

    def test_expected_ips_parsing_single_ip(self, service):
        """Test expected_ips parsing with single IP."""
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100",
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert form.is_valid(), form.errors
        expected_ips = form.cleaned_data["expected_ips"]
        assert expected_ips == ["192.168.1.100"]

    def test_expected_ips_parsing_multiple_ips(self, service):
        """Test expected_ips parsing with multiple IPs (one per line)."""
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100\n192.168.1.101\n192.168.1.102",
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert form.is_valid(), form.errors
        expected_ips = form.cleaned_data["expected_ips"]
        assert expected_ips == ["192.168.1.100", "192.168.1.101", "192.168.1.102"]

    def test_expected_ips_invalid_ip(self, service):
        """Test that invalid IP addresses fail validation."""
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100\ninvalid-ip\n192.168.1.102",
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert not form.is_valid()
        assert "expected_ips" in form.errors
        assert "invalid-ip" in str(form.errors["expected_ips"])

    def test_expected_ips_required(self, service):
        """Test that expected_ips is required for DNS checks."""
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "",  # Empty
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert not form.is_valid()
        assert "expected_ips" in form.errors

    def test_timeout_field_defaults_and_constraints(self, service):
        """Test timeout field has proper default and constraints."""
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100",
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["timeout"] == 5.0

        # Test min value constraint
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100",
                "query_type": "A",
                "timeout": 0.05,  # Below min
            }
        )
        assert not form.is_valid()
        assert "timeout" in form.errors

    def test_query_type_validation_a_and_aaaa(self, service):
        """Test that only A and AAAA query types are accepted."""
        # A record should work
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100",
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert form.is_valid(), form.errors

        # AAAA record should work
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "2001:db8::1",
                "query_type": "AAAA",
                "timeout": 5.0,
            }
        )
        assert form.is_valid(), form.errors

    def test_data_jsonfield_serialization(self, service):
        """Test that data JSONField is properly serialized in save()."""
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100\n192.168.1.101",
                "dns_server": "8.8.8.8",
                "source_ip": "192.168.1.50",
                "query_type": "A",
                "timeout": 10.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()

        assert instance.data == {
            "expected_ips": ["192.168.1.100", "192.168.1.101"],
            "query_type": "A",
            "timeout": 10.0,
            "dns_server": "8.8.8.8",
            "source_ip": "192.168.1.50",
        }

    def test_editing_existing_dns_check_populates_fields(self, service):
        """Test that editing an existing DNS check populates fields from data."""
        # Create a DNS check with data
        health_check = HealthCheck.objects.create(
            name="Existing DNS Check",
            service=service,
            check_type=CheckType.DNS,
            url="example.com",
            check_interval=300,
            data={
                "expected_ips": ["192.168.1.100", "192.168.1.101"],
                "dns_server": "8.8.8.8",
                "source_ip": "192.168.1.50",
                "query_type": "A",
                "timeout": 10.0,
            },
        )

        # Create form with instance
        form = DnsHealthCheckForm(instance=health_check)

        # Check that fields are populated
        assert form.fields["expected_ips"].initial == "192.168.1.100\n192.168.1.101"
        assert form.fields["dns_server"].initial == "8.8.8.8"
        assert form.fields["source_ip"].initial == "192.168.1.50"
        assert form.fields["query_type"].initial == "A"
        assert form.fields["timeout"].initial == 10.0

    def test_check_type_auto_set_to_dns(self, service):
        """Test that check_type is automatically set to DNS."""
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.DNS,
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100",
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.check_type == CheckType.DNS

    def test_url_field_label_is_domain(self, service):
        """Test that url field label is 'Domain' not 'URL'."""
        form = DnsHealthCheckForm()
        assert form.fields["url"].label == "Domain"

    def test_check_type_tampering_prevention(self, service):
        """Test that check_type is explicitly set in save() to prevent tampering."""
        # Try to set check_type to HTTP in POST data
        form = DnsHealthCheckForm(
            data={
                "name": "Test DNS Check",
                "service": service.id,
                "check_type": CheckType.HTTP,  # Attempt tampering
                "url": "example.com",
                "check_interval": 300,
                "disabled": False,
                "expected_ips": "192.168.1.100",
                "query_type": "A",
                "timeout": 5.0,
            }
        )
        assert form.is_valid()
        instance = form.save()
        # Should still be DNS, not HTTP
        assert instance.check_type == CheckType.DNS


class TestGenericHealthCheckForm:
    """Tests for GenericHealthCheckForm used for unmapped check types (TCP, Ping, etc.)."""

    def test_unmapped_check_type_preserved_on_edit(self, service):
        """Test that unmapped check types (TCP, Ping, etc.) are preserved when editing."""
        # Create a TCP check (unmapped type)
        health_check = HealthCheck.objects.create(
            name="Existing TCP Check",
            service=service,
            check_type=CheckType.TCP,
            url="tcp://example.com:3306",
            check_interval=300,
        )

        # Edit it via generic form (fallback for unmapped types)
        form = GenericHealthCheckForm(
            data={
                "name": "Updated TCP Check",
                "service": service.id,
                "check_type": CheckType.TCP,
                "url": "tcp://example.com:3307",
                "check_interval": 300,
                "disabled": False,
            },
            instance=health_check,
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        # Should still be TCP
        assert instance.check_type == CheckType.TCP
        assert instance.url == "tcp://example.com:3307"

    def test_unmapped_check_type_created_with_initial_data(self, service):
        """Test that unmapped check types can be created via initial data."""
        form = GenericHealthCheckForm(
            data={
                "name": "New Ping Check",
                "service": service.id,
                "check_type": CheckType.PING,
                "url": "8.8.8.8",
                "check_interval": 300,
                "disabled": False,
            },
            initial={"check_type": CheckType.PING},
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.check_type == CheckType.PING
        assert instance.url == "8.8.8.8"

    def test_data_field_preserved_on_edit(self, service):
        """Test that existing data field is preserved when editing unmapped types."""
        # Create a TCP check with custom data (e.g., timeout config)
        tcp_config = {"timeout": 10, "port": 3306}
        health_check = HealthCheck.objects.create(
            name="TCP Check with Config",
            service=service,
            check_type=CheckType.TCP,
            url="tcp://example.com:3306",
            check_interval=300,
            data=tcp_config,
        )

        # Edit basic fields via generic form
        form = GenericHealthCheckForm(
            data={
                "name": "Updated TCP Check",
                "service": service.id,
                "check_type": CheckType.TCP,
                "url": "tcp://example.com:3307",
                "check_interval": 300,  # Keep same valid interval
                "disabled": False,
            },
            instance=health_check,
        )
        assert form.is_valid(), form.errors
        instance = form.save()

        # Data field should be preserved (not wiped)
        assert instance.data == tcp_config
        # But basic fields should be updated
        assert instance.name == "Updated TCP Check"
        assert instance.url == "tcp://example.com:3307"

    def test_data_field_empty_on_create(self, service):
        """Test that new checks get empty data dict by default."""
        form = GenericHealthCheckForm(
            data={
                "name": "New Custom Check",
                "service": service.id,
                "check_type": CheckType.CUSTOM,
                "url": "custom://test",
                "check_interval": 300,
                "disabled": False,
            },
            initial={"check_type": CheckType.CUSTOM},
        )
        assert form.is_valid(), form.errors
        instance = form.save()

        # New checks should have empty data dict (model default)
        assert instance.data == {}
