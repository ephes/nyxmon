"""Tests for Django forms."""

import pytest

from nyxboard.forms import (
    HttpHealthCheckForm,
    DnsHealthCheckForm,
    SmtpHealthCheckForm,
    ImapHealthCheckForm,
    TcpHealthCheckForm,
    JsonMetricsHealthCheckForm,
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


class TestSmtpHealthCheckForm:
    """Tests for SmtpHealthCheckForm."""

    def test_check_type_auto_set_to_smtp(self, service):
        """Test that check_type is automatically set to SMTP."""
        form = SmtpHealthCheckForm(
            data={
                "name": "Test SMTP Check",
                "service": service.id,
                "check_type": CheckType.SMTP,
                "url": "smtp://mail.example.com:587",
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.check_type == CheckType.SMTP

    def test_check_type_hidden_in_form(self, service):
        """Test that check_type field is hidden."""
        form = SmtpHealthCheckForm()
        assert form.fields["check_type"].widget.__class__.__name__ == "HiddenInput"

    def test_url_auto_generated_from_host_port(self, service):
        """Test that url is auto-generated from host and port."""
        form = SmtpHealthCheckForm(
            data={
                "name": "Test SMTP Check",
                "service": service.id,
                "check_type": CheckType.SMTP,
                "url": "ignored",  # Will be overwritten
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 465,
                "tls_mode": "implicit",
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.url == "mail.example.com"

    def test_data_jsonfield_serialization(self, service):
        """Test that data JSONField is properly serialized in save()."""
        form = SmtpHealthCheckForm(
            data={
                "name": "Test SMTP Check",
                "service": service.id,
                "check_type": CheckType.SMTP,
                "url": "smtp://mail.example.com:587",
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "username": "user@example.com",
                "password": "secret123",
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon-test]",
                "timeout": 45.0,
                "retries": 3,
                "retry_delay": 10.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()

        assert instance.data["host"] == "mail.example.com"
        assert instance.data["port"] == 587
        assert instance.data["tls"] == "starttls"
        assert instance.data["username"] == "user@example.com"
        assert instance.data["password"] == "secret123"
        assert instance.data["from_addr"] == "monitor@example.com"
        assert instance.data["to_addr"] == "test@example.com"
        assert instance.data["subject_prefix"] == "[nyxmon-test]"
        assert instance.data["timeout"] == 45.0
        assert instance.data["retries"] == 3
        assert instance.data["retry_delay"] == 10.0

    def test_password_required_when_username_provided(self, service):
        """Test that password is required when username is provided."""
        form = SmtpHealthCheckForm(
            data={
                "name": "Test SMTP Check",
                "service": service.id,
                "check_type": CheckType.SMTP,
                "url": "smtp://mail.example.com:587",
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "username": "user@example.com",
                "password": "",  # Empty password with username
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            }
        )
        assert not form.is_valid()
        assert "password" in form.errors

    def test_password_not_required_without_username(self, service):
        """Test that password is not required when username is empty."""
        form = SmtpHealthCheckForm(
            data={
                "name": "Test SMTP Check",
                "service": service.id,
                "check_type": CheckType.SMTP,
                "url": "smtp://mail.example.com:25",
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 25,
                "tls_mode": "none",
                "username": "",  # No username
                "password": "",  # No password - should be fine
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            }
        )
        assert form.is_valid(), form.errors

    def test_editing_existing_smtp_check_populates_fields(self, service):
        """Test that editing an existing SMTP check populates fields from data."""
        health_check = HealthCheck.objects.create(
            name="Existing SMTP Check",
            service=service,
            check_type=CheckType.SMTP,
            url="smtp://mail.example.com:587",
            check_interval=300,
            data={
                "host": "mail.example.com",
                "port": 587,
                "tls": "starttls",
                "username": "user@example.com",
                "password": "secret123",
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            },
        )

        form = SmtpHealthCheckForm(instance=health_check)

        assert form.fields["host"].initial == "mail.example.com"
        assert form.fields["port"].initial == 587
        assert form.fields["tls_mode"].initial == "starttls"
        assert form.fields["username"].initial == "user@example.com"
        # Password should NOT be populated for security
        assert form.fields["from_addr"].initial == "monitor@example.com"
        assert form.fields["to_addr"].initial == "test@example.com"

    def test_password_preserved_when_editing_without_new_password(self, service):
        """Test that existing password is preserved when editing without providing new one."""
        health_check = HealthCheck.objects.create(
            name="Existing SMTP Check",
            service=service,
            check_type=CheckType.SMTP,
            url="smtp://mail.example.com:587",
            check_interval=300,
            data={
                "host": "mail.example.com",
                "port": 587,
                "tls": "starttls",
                "username": "user@example.com",
                "password": "original_secret",
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            },
        )

        form = SmtpHealthCheckForm(
            data={
                "name": "Updated SMTP Check",
                "service": service.id,
                "check_type": CheckType.SMTP,
                "url": "smtp://mail.example.com:587",
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "username": "user@example.com",
                "password": "",  # Empty - should keep existing
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            },
            instance=health_check,
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.data["password"] == "original_secret"

    def test_check_type_tampering_prevention(self, service):
        """Test that check_type is explicitly set in save() to prevent tampering."""
        form = SmtpHealthCheckForm(
            data={
                "name": "Test SMTP Check",
                "service": service.id,
                "check_type": CheckType.HTTP,  # Attempt tampering
                "url": "smtp://mail.example.com:587",
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "from_addr": "monitor@example.com",
                "to_addr": "test@example.com",
                "subject_prefix": "[nyxmon]",
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 5.0,
            }
        )
        assert form.is_valid()
        instance = form.save()
        assert instance.check_type == CheckType.SMTP


class TestImapHealthCheckForm:
    """Tests for ImapHealthCheckForm."""

    def test_check_type_auto_set_to_imap(self, service):
        """Test that check_type is automatically set to IMAP."""
        form = ImapHealthCheckForm(
            data={
                "name": "Test IMAP Check",
                "service": service.id,
                "check_type": CheckType.IMAP,
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 993,
                "tls_mode": "implicit",
                "username": "user@example.com",
                "password": "secret123",
                "folder": "INBOX",
                "search_subject": "[nyxmon]",
                "max_age_minutes": 30,
                "delete_after_check": True,
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 10.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.check_type == CheckType.IMAP

    def test_hidden_fields_configuration(self, service):
        """Test that check_type and url fields are hidden and url is optional."""
        form = ImapHealthCheckForm()
        assert form.fields["check_type"].widget.__class__.__name__ == "HiddenInput"
        assert form.fields["url"].widget.__class__.__name__ == "HiddenInput"
        assert form.fields["url"].required is False

    def test_url_auto_generated_from_host_port(self, service):
        """Test that url is auto-generated from host and port."""
        form = ImapHealthCheckForm(
            data={
                "name": "Test IMAP Check",
                "service": service.id,
                "check_type": CheckType.IMAP,
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 143,
                "tls_mode": "starttls",
                "username": "user@example.com",
                "password": "secret123",
                "folder": "INBOX",
                "search_subject": "[nyxmon]",
                "max_age_minutes": 30,
                "delete_after_check": True,
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 10.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.url == "mail.example.com"

    def test_data_jsonfield_serialization(self, service):
        """Test that data JSONField is properly serialized in save()."""
        form = ImapHealthCheckForm(
            data={
                "name": "Test IMAP Check",
                "service": service.id,
                "check_type": CheckType.IMAP,
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 993,
                "tls_mode": "implicit",
                "username": "user@example.com",
                "password": "secret123",
                "folder": "Monitoring",
                "search_subject": "[nyxmon-test]",
                "max_age_minutes": 60,
                "delete_after_check": False,
                "timeout": 45.0,
                "retries": 3,
                "retry_delay": 15.0,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()

        assert instance.data["host"] == "mail.example.com"
        assert instance.data["port"] == 993
        assert instance.data["tls_mode"] == "implicit"
        assert instance.data["username"] == "user@example.com"
        assert instance.data["password"] == "secret123"
        assert instance.data["folder"] == "Monitoring"
        assert instance.data["search_subject"] == "[nyxmon-test]"
        assert instance.data["max_age_minutes"] == 60
        assert instance.data["delete_after_check"] is False
        assert instance.data["timeout"] == 45.0
        assert instance.data["retries"] == 3
        assert instance.data["retry_delay"] == 15.0

    def test_password_required_for_imap(self, service):
        """Test that password is required for IMAP checks."""
        form = ImapHealthCheckForm(
            data={
                "name": "Test IMAP Check",
                "service": service.id,
                "check_type": CheckType.IMAP,
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 993,
                "tls_mode": "implicit",
                "username": "user@example.com",
                "password": "",  # Empty password
                "folder": "INBOX",
                "search_subject": "[nyxmon]",
                "max_age_minutes": 30,
                "delete_after_check": True,
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 10.0,
            }
        )
        assert not form.is_valid()
        assert "password" in form.errors

    def test_editing_existing_imap_check_populates_fields(self, service):
        """Test that editing an existing IMAP check populates fields from data."""
        health_check = HealthCheck.objects.create(
            name="Existing IMAP Check",
            service=service,
            check_type=CheckType.IMAP,
            url="imap://mail.example.com:993",
            check_interval=300,
            data={
                "host": "mail.example.com",
                "port": 993,
                "tls_mode": "implicit",
                "username": "user@example.com",
                "password": "secret123",
                "folder": "Monitoring",
                "search_subject": "[nyxmon]",
                "max_age_minutes": 45,
                "delete_after_check": False,
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 10.0,
            },
        )

        form = ImapHealthCheckForm(instance=health_check)

        assert form.fields["host"].initial == "mail.example.com"
        assert form.fields["port"].initial == 993
        assert form.fields["tls_mode"].initial == "implicit"
        assert form.fields["username"].initial == "user@example.com"
        assert form.fields["folder"].initial == "Monitoring"
        assert form.fields["search_subject"].initial == "[nyxmon]"
        assert form.fields["max_age_minutes"].initial == 45
        assert form.fields["delete_after_check"].initial is False

    def test_password_preserved_when_editing_without_new_password(self, service):
        """Test that existing password is preserved when editing without providing new one."""
        health_check = HealthCheck.objects.create(
            name="Existing IMAP Check",
            service=service,
            check_type=CheckType.IMAP,
            url="imap://mail.example.com:993",
            check_interval=300,
            data={
                "host": "mail.example.com",
                "port": 993,
                "tls_mode": "implicit",
                "username": "user@example.com",
                "password": "original_secret",
                "folder": "INBOX",
                "search_subject": "[nyxmon]",
                "max_age_minutes": 30,
                "delete_after_check": True,
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 10.0,
            },
        )

        form = ImapHealthCheckForm(
            data={
                "name": "Updated IMAP Check",
                "service": service.id,
                "check_type": CheckType.IMAP,
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 993,
                "tls_mode": "implicit",
                "username": "user@example.com",
                "password": "",  # Empty - should keep existing
                "folder": "INBOX",
                "search_subject": "[nyxmon]",
                "max_age_minutes": 30,
                "delete_after_check": True,
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 10.0,
            },
            instance=health_check,
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.data["password"] == "original_secret"

    def test_check_type_tampering_prevention(self, service):
        """Test that check_type is explicitly set in save() to prevent tampering."""
        form = ImapHealthCheckForm(
            data={
                "name": "Test IMAP Check",
                "service": service.id,
                "check_type": CheckType.HTTP,  # Attempt tampering
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 993,
                "tls_mode": "implicit",
                "username": "user@example.com",
                "password": "secret123",
                "folder": "INBOX",
                "search_subject": "[nyxmon]",
                "max_age_minutes": 30,
                "delete_after_check": True,
                "timeout": 30.0,
                "retries": 2,
                "retry_delay": 10.0,
            }
        )
        assert form.is_valid()
        instance = form.save()
        assert instance.check_type == CheckType.IMAP


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
                "name": "New Ping Check",
                "service": service.id,
                "check_type": CheckType.PING,
                "url": "192.168.1.1",
                "check_interval": 300,
                "disabled": False,
            },
            initial={"check_type": CheckType.PING},
        )
        assert form.is_valid(), form.errors
        instance = form.save()

        # New checks should have empty data dict (model default)
        assert instance.data == {}


class TestGenericHealthCheckFormLegacyTypes:
    """Tests for GenericHealthCheckForm handling of legacy/deprecated check types."""

    def test_legacy_check_type_preserved_when_browser_omits_field(self, service):
        """Regression test: editing legacy instance where browser omits check_type from POST.

        When a field has `disabled=True` at the Django level, browsers don't submit
        it in POST data. This test verifies the form correctly preserves the legacy
        check_type value from the instance in this scenario.
        """
        # Create a legacy 'custom' check in the database
        # Note: In real usage, this would be a pre-existing check from before
        # the CustomExecutor was removed. We simulate by directly setting check_type.
        legacy_check = HealthCheck.objects.create(
            name="Legacy Custom Check",
            service=service,
            check_type="custom",  # Legacy type not in SUPPORTED_CHECK_TYPES
            url="legacy://example.com",
            check_interval=300,
            data={"host": "example.com", "command": "test"},
        )

        # Simulate browser POST where disabled field is omitted
        # (no check_type in POST data, as browsers don't submit disabled fields)
        form = GenericHealthCheckForm(
            data={
                "name": "Updated Legacy Check",
                "service": service.id,
                # check_type intentionally omitted - browser won't send disabled field
                "url": "legacy://example.com",
                "check_interval": 300,
                "disabled": True,  # Operator disables the legacy check
            },
            instance=legacy_check,
        )

        assert form.is_valid(), form.errors
        instance = form.save()

        # Critical: check_type must be preserved as 'custom', not changed/lost
        assert instance.check_type == "custom"
        assert instance.name == "Updated Legacy Check"
        assert instance.disabled is True

    def test_legacy_check_type_field_is_disabled(self, service):
        """Test that check_type field is disabled for legacy checks."""
        legacy_check = HealthCheck.objects.create(
            name="Legacy Custom Check",
            service=service,
            check_type="custom",
            url="legacy://example.com",
            check_interval=300,
        )

        form = GenericHealthCheckForm(instance=legacy_check)

        # Field should be disabled at Django level
        assert form.fields["check_type"].disabled is True
        # Legacy type should be in choices
        choices = dict(form.fields["check_type"].choices)
        assert "custom" in choices
        assert "DEPRECATED" in choices["custom"]

    def test_legacy_check_type_not_added_on_create(self, service):
        """Test that legacy types cannot be used to create new checks.

        This prevents ?type=custom URL parameter from creating new legacy checks.
        """
        # Try to create a new check with a legacy type via initial data
        form = GenericHealthCheckForm(
            data={
                "name": "New Check",
                "service": service.id,
                "check_type": "custom",  # Attempt to use legacy type
                "url": "http://example.com",
                "check_interval": 300,
                "disabled": False,
            },
            initial={"check_type": "custom"},  # Simulate ?type=custom
        )

        # Form should not add 'custom' to choices for new instances
        choices = dict(form.fields["check_type"].choices)
        assert "custom" not in choices

        # Form should be invalid because 'custom' is not a valid choice
        assert not form.is_valid()
        assert "check_type" in form.errors

    def test_legacy_check_shows_deprecation_warning(self, service):
        """Test that editing legacy checks shows deprecation warning."""
        legacy_check = HealthCheck.objects.create(
            name="Legacy Custom Check",
            service=service,
            check_type="custom",
            url="legacy://example.com",
            check_interval=300,
        )

        form = GenericHealthCheckForm(instance=legacy_check)

        assert len(form.warnings) == 1
        assert "deprecated" in form.warnings[0].lower()
        assert "custom" in form.warnings[0]

    def test_no_warning_for_supported_check_types(self, service):
        """Test that supported check types don't show deprecation warning."""
        tcp_check = HealthCheck.objects.create(
            name="TCP Check",
            service=service,
            check_type=CheckType.TCP,
            url="example.com",
            check_interval=300,
        )

        form = GenericHealthCheckForm(instance=tcp_check)

        assert len(form.warnings) == 0
        assert form.fields["check_type"].disabled is False


class TestTcpHealthCheckForm:
    """Tests for TcpHealthCheckForm warnings and serialization."""

    def test_starttls_is_valid_but_warns(self, service):
        form = TcpHealthCheckForm(
            data={
                "name": "SMTP STARTTLS probe",
                "service": service.id,
                "check_type": CheckType.TCP,
                "check_interval": 300,
                "disabled": False,
                "host": "mail.example.com",
                "port": 587,
                "tls_mode": "starttls",
                "connect_timeout": 10.0,
                "tls_handshake_timeout": 10.0,
                "retries": 1,
                "retry_delay": 0.0,
                "check_cert_expiry": True,
                "min_cert_days": 14,
                "sni": "",
                "verify": True,
            }
        )
        assert form.is_valid(), form.errors
        assert hasattr(form, "warnings")
        assert any("STARTTLS is a generic probe" in w for w in form.warnings)
        assert any("Port 587 typically expects" in w for w in form.warnings)

    def test_none_tls_disables_cert_expiry(self, service):
        form = TcpHealthCheckForm(
            data={
                "name": "Plain TCP",
                "service": service.id,
                "check_type": CheckType.TCP,
                "check_interval": 300,
                "disabled": False,
                "host": "example.com",
                "port": 25,
                "tls_mode": "none",
                "connect_timeout": 10.0,
                "tls_handshake_timeout": 10.0,
                "retries": 0,
                "retry_delay": 0.0,
                "check_cert_expiry": True,  # should be cleared
                "min_cert_days": 14,
                "sni": "",
                "verify": True,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.data["check_cert_expiry"] is False


class TestJsonMetricsHealthCheckForm:
    """Tests for JsonMetricsHealthCheckForm parsing and serialization."""

    def test_requires_checks_json_to_be_valid_json(self, service):
        form = JsonMetricsHealthCheckForm(
            data={
                "name": "Mail metrics thresholds",
                "service": service.id,
                "check_type": CheckType.JSON_METRICS,
                "check_interval": 300,
                "disabled": False,
                "url": "http://localhost:9100/.well-known/health",
                "auth_username": "",
                "auth_password": "",
                "timeout": 10.0,
                "retries": 1,
                "retry_delay": 2.0,
                "checks_json": "{not json}",
            }
        )
        assert not form.is_valid()
        assert "checks_json" in form.errors

    def test_serializes_checks_and_optional_auth(self, service):
        checks_json = """
[
  {"path": "$.mail.queue_total", "op": "<", "value": 100, "severity": "warning"}
]
""".strip()
        form = JsonMetricsHealthCheckForm(
            data={
                "name": "Mail metrics thresholds",
                "service": service.id,
                "check_type": CheckType.JSON_METRICS,
                "check_interval": 300,
                "disabled": False,
                "url": "http://localhost:9100/.well-known/health",
                "auth_username": "nyxmon",
                "auth_password": "secret",
                "timeout": 10.0,
                "retries": 1,
                "retry_delay": 2.0,
                "checks_json": checks_json,
            }
        )
        assert form.is_valid(), form.errors
        instance = form.save()
        assert instance.check_type == CheckType.JSON_METRICS
        assert instance.url == "http://localhost:9100/.well-known/health"
        assert instance.data["checks"][0]["path"] == "$.mail.queue_total"
        assert instance.data["auth"]["username"] == "nyxmon"
        assert instance.data["auth"]["password"] == "secret"
