from typing import Any

from django import forms
from django.core.validators import URLValidator
import json

from .models import Service, HealthCheck
from nyxmon.domain import CheckType


class ServiceForm(forms.ModelForm):
    """Form for creating and updating Service objects."""

    class Meta:
        model = Service
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"class": "form-control"})}


class HealthCheckForm(forms.ModelForm):
    """Base form for all health checks.

    Contains only fields shared across all check types:
    - name, service, check_type, url, check_interval, disabled

    Note: 'url' field semantics vary by check type (URL for HTTP, domain for DNS).
    Subclasses may customize label and help text.
    """

    class Meta:
        model = HealthCheck
        fields = ["name", "service", "check_type", "url", "check_interval", "disabled"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. Homepage Availability",
                }
            ),
            "service": forms.Select(attrs={"class": "form-control"}),
            "check_type": forms.Select(attrs={"class": "form-control"}),
            "url": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://example.com/api/health",
                }
            ),
            "check_interval": forms.Select(attrs={"class": "form-control"}),
            "disabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
        help_texts = {
            "name": "A descriptive name for this health check.",
            "check_type": "Select the type of health check to perform.",
            "url": "Enter the URL to check for HTTP health checks.",
            "check_interval": "Select how frequently this health check should run.",
            "disabled": "Check this box to temporarily disable this health check without deleting it.",
        }


class GenericHealthCheckForm(HealthCheckForm):
    """Form for unmapped health check types (TCP, Ping, Custom, etc.).

    This form preserves the existing check_type and data field without modification.
    It's used as a fallback for check types that don't have specialized forms yet.

    This prevents data loss when editing legacy or future check types before
    dedicated forms are implemented.
    """

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Preserve existing data field - don't modify it
        # TCP/Ping/Custom checks may store type-specific config here
        # that we don't want to lose when editing basic fields

        # Note: instance.data is already set from the database when editing,
        # or from model default ({}) when creating. We don't touch it.

        if commit:
            instance.save()

        return instance


class HttpHealthCheckForm(HealthCheckForm):
    """Form for HTTP and JSON-HTTP health checks.

    This form is used for both HTTP and JSON-HTTP check types. The target
    check_type is preserved from the instance when editing, or determined
    from initial data when creating.

    Currently has no additional fields beyond base form.
    Future: Could add timeout, expected status codes, etc.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Determine target check type
        # Priority: instance (editing) > POST data (form submission) > initial data (form display) > default
        if self.instance.pk and self.instance.check_type in (
            CheckType.HTTP,
            CheckType.JSON_HTTP,
        ):
            # Editing existing check - preserve its type (HTTP or JSON-HTTP)
            self._target_check_type = self.instance.check_type
        else:
            # Creating new check - check POST data first, then initial data
            check_type_from_data = self.data.get("check_type") if self.data else None
            check_type_from_initial = self.initial.get("check_type")

            if check_type_from_data in (CheckType.HTTP, CheckType.JSON_HTTP):
                # Use type from POST data (from hidden field in submitted form)
                self._target_check_type = check_type_from_data
            elif check_type_from_initial in (CheckType.HTTP, CheckType.JSON_HTTP):
                # Use type from initial data (GET request, will be rendered in form)
                self._target_check_type = check_type_from_initial
            else:
                # Fallback default to HTTP
                self._target_check_type = CheckType.HTTP

        # Force check_type to target type (prevents tampering to incompatible types like DNS)
        self.fields["check_type"].initial = self._target_check_type
        self.fields["check_type"].widget = forms.HiddenInput()

        # Customize url field for HTTP semantics
        self.fields["url"].label = "URL"
        self.fields["url"].help_text = "Full URL to check (e.g., https://example.com)"

        # Re-apply URL validation since model field is now CharField
        # More declarative than overriding clean_url()
        self.fields["url"].validators.append(URLValidator())

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Explicitly set check_type to target type (prevents tampering, preserves JSON-HTTP)
        # This uses the type determined in __init__, not from POST data
        instance.check_type = self._target_check_type

        # HTTP checks don't need data field currently
        # Future: Could store timeout, expected_status, etc.
        instance.data = {}

        if commit:
            instance.save()

        return instance


class SmtpHealthCheckForm(HealthCheckForm):
    """Form for SMTP health checks.

    Adds SMTP-specific fields and validation.
    Handles serialization to/from HealthCheck.data JSONField.
    """

    # SMTP-specific fields (not in model, stored in data JSONField)
    host = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "mail.example.com"}
        ),
        label="SMTP Host",
        help_text="SMTP server hostname",
    )

    port = forms.IntegerField(
        initial=587,
        min_value=1,
        max_value=65535,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Port",
        help_text="SMTP port (587 for STARTTLS, 465 for implicit TLS, 25 for plain)",
    )

    tls_mode = forms.ChoiceField(
        choices=[
            ("starttls", "STARTTLS (port 587)"),
            ("implicit", "Implicit TLS (port 465)"),
            ("none", "None (port 25)"),
        ],
        initial="starttls",
        widget=forms.Select(attrs={"class": "form-control"}),
        label="TLS Mode",
        help_text="TLS encryption mode for the connection",
    )

    username = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "user@example.com"}
        ),
        label="Username",
        help_text="SMTP authentication username (optional)",
    )

    password = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Leave blank to keep existing",
            }
        ),
        label="Password",
        help_text="SMTP authentication password (optional)",
    )

    from_addr = forms.EmailField(
        widget=forms.EmailInput(
            attrs={"class": "form-control", "placeholder": "monitor@example.com"}
        ),
        label="From Address",
        help_text="Sender email address for test messages",
    )

    to_addr = forms.EmailField(
        widget=forms.EmailInput(
            attrs={"class": "form-control", "placeholder": "test@example.com"}
        ),
        label="To Address",
        help_text="Recipient email address for test messages",
    )

    subject_prefix = forms.CharField(
        max_length=100,
        initial="[nyxmon]",
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="Subject Prefix",
        help_text="Prefix for test email subjects (used by IMAP check to find messages)",
    )

    timeout = forms.FloatField(
        initial=30.0,
        min_value=1.0,
        max_value=300.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Timeout (seconds)",
        help_text="Connection timeout in seconds",
    )

    retries = forms.IntegerField(
        initial=2,
        min_value=0,
        max_value=10,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Retries",
        help_text="Number of retry attempts on failure",
    )

    retry_delay = forms.FloatField(
        initial=5.0,
        min_value=0.0,
        max_value=60.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Retry Delay (seconds)",
        help_text="Delay between retry attempts",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Force check_type to SMTP
        self.fields["check_type"].initial = CheckType.SMTP
        self.fields["check_type"].widget = forms.HiddenInput()

        # Hide URL field; host is the source of truth
        self.fields["url"].required = False
        self.fields["url"].widget = forms.HiddenInput()
        if self.instance.pk and self.instance.url:
            self.fields["url"].initial = self.instance.url

        # If editing existing SMTP check, populate fields from data
        if self.instance.pk and self.instance.data:
            smtp_config = self.instance.data
            self.fields["host"].initial = smtp_config.get("host", "")
            self.fields["port"].initial = smtp_config.get("port", 587)
            self.fields["tls_mode"].initial = smtp_config.get("tls", "starttls")
            self.fields["username"].initial = smtp_config.get("username", "")
            # Don't populate password - security best practice
            self.fields["from_addr"].initial = smtp_config.get("from_addr", "")
            self.fields["to_addr"].initial = smtp_config.get("to_addr", "")
            self.fields["subject_prefix"].initial = smtp_config.get(
                "subject_prefix", "[nyxmon]"
            )
            self.fields["timeout"].initial = smtp_config.get("timeout", 30.0)
            self.fields["retries"].initial = smtp_config.get("retries", 2)
            self.fields["retry_delay"].initial = smtp_config.get("retry_delay", 5.0)

    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get("username")
        password = cleaned_data.get("password")

        # If username is provided, password is required (unless editing and keeping existing)
        if username and not password:
            # Check if we're editing and have existing password
            if self.instance.pk and self.instance.data:
                existing_password = self.instance.data.get("password")
                if not existing_password:
                    self.add_error(
                        "password", "Password is required when username is provided"
                    )
            else:
                self.add_error(
                    "password", "Password is required when username is provided"
                )

        return cleaned_data

    def save(self, commit=True):
        # Save existing data before super().save() modifies instance
        existing_data = (
            self.instance.data.copy() if self.instance.pk and self.instance.data else {}
        )

        instance = super().save(commit=False)

        # Explicitly set check_type to prevent tampering
        instance.check_type = CheckType.SMTP

        # Build URL from host and port
        host = self.cleaned_data["host"]
        port = self.cleaned_data["port"]
        instance.url = host

        # Populate data JSONField from form fields
        instance.data = {
            "host": host,
            "port": port,
            "tls": self.cleaned_data["tls_mode"],
            "from_addr": self.cleaned_data["from_addr"],
            "to_addr": self.cleaned_data["to_addr"],
            "subject_prefix": self.cleaned_data["subject_prefix"],
            "timeout": self.cleaned_data["timeout"],
            "retries": self.cleaned_data["retries"],
            "retry_delay": self.cleaned_data["retry_delay"],
        }

        # Add optional auth fields
        if self.cleaned_data.get("username"):
            instance.data["username"] = self.cleaned_data["username"]

        # Handle password - keep existing if not provided
        if self.cleaned_data.get("password"):
            instance.data["password"] = self.cleaned_data["password"]
        elif existing_data.get("password"):
            # Preserve existing password when editing
            instance.data["password"] = existing_data["password"]

        if commit:
            instance.save()

        return instance


class ImapHealthCheckForm(HealthCheckForm):
    """Form for IMAP health checks.

    Adds IMAP-specific fields and validation.
    Handles serialization to/from HealthCheck.data JSONField.
    """

    # IMAP-specific fields (not in model, stored in data JSONField)
    host = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "mail.example.com"}
        ),
        label="IMAP Host",
        help_text="IMAP server hostname (uses url field for display)",
    )

    port = forms.IntegerField(
        initial=993,
        min_value=1,
        max_value=65535,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Port",
        help_text="IMAP port (993 for implicit TLS, 143 for STARTTLS/plain)",
    )

    tls_mode = forms.ChoiceField(
        choices=[
            ("implicit", "Implicit TLS (port 993)"),
            ("starttls", "STARTTLS (port 143)"),
            ("none", "None (port 143)"),
        ],
        initial="implicit",
        widget=forms.Select(attrs={"class": "form-control"}),
        label="TLS Mode",
        help_text="TLS encryption mode for the connection",
    )

    username = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "user@example.com"}
        ),
        label="Username",
        help_text="IMAP login username",
    )

    password = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Leave blank to keep existing",
            }
        ),
        label="Password",
        help_text="IMAP login password",
    )

    folder = forms.CharField(
        max_length=255,
        initial="INBOX",
        widget=forms.TextInput(attrs={"class": "form-control"}),
        label="Folder",
        help_text="IMAP folder to search for test messages",
    )

    search_subject = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "[nyxmon]"}
        ),
        label="Search Subject",
        help_text="Subject pattern to search for (should match SMTP subject prefix)",
    )

    max_age_minutes = forms.IntegerField(
        initial=30,
        min_value=1,
        max_value=1440,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Max Age (minutes)",
        help_text="Maximum age of messages to consider valid",
    )

    delete_after_check = forms.BooleanField(
        initial=True,
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Delete After Check",
        help_text="Delete matched messages after successful check",
    )

    timeout = forms.FloatField(
        initial=30.0,
        min_value=1.0,
        max_value=300.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Timeout (seconds)",
        help_text="Connection timeout in seconds",
    )

    retries = forms.IntegerField(
        initial=2,
        min_value=0,
        max_value=10,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Retries",
        help_text="Number of retry attempts on failure",
    )

    retry_delay = forms.FloatField(
        initial=10.0,
        min_value=0.0,
        max_value=60.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Retry Delay (seconds)",
        help_text="Delay between retry attempts",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Force check_type to IMAP
        self.fields["check_type"].initial = CheckType.IMAP
        self.fields["check_type"].widget = forms.HiddenInput()

        # Hide URL field - IMAP host/port is the source of truth
        self.fields["url"].required = False
        self.fields["url"].widget = forms.HiddenInput()
        if self.instance.pk and self.instance.url:
            self.fields["url"].initial = self.instance.url

        # If editing existing IMAP check, populate fields from data
        if self.instance.pk and self.instance.data:
            imap_config = self.instance.data
            # Host comes from url field parsing or direct storage
            if "host" in imap_config:
                self.fields["host"].initial = imap_config.get("host", "")
            self.fields["port"].initial = imap_config.get("port", 993)
            self.fields["tls_mode"].initial = imap_config.get("tls_mode", "implicit")
            self.fields["username"].initial = imap_config.get("username", "")
            # Don't populate password - security best practice
            self.fields["folder"].initial = imap_config.get("folder", "INBOX")
            self.fields["search_subject"].initial = imap_config.get(
                "search_subject", ""
            )
            self.fields["max_age_minutes"].initial = imap_config.get(
                "max_age_minutes", 30
            )
            self.fields["delete_after_check"].initial = imap_config.get(
                "delete_after_check", True
            )
            self.fields["timeout"].initial = imap_config.get("timeout", 30.0)
            self.fields["retries"].initial = imap_config.get("retries", 2)
            self.fields["retry_delay"].initial = imap_config.get("retry_delay", 10.0)

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")

        # Password is required for IMAP (unless editing and keeping existing)
        if not password:
            if self.instance.pk and self.instance.data:
                existing_password = self.instance.data.get("password")
                if not existing_password:
                    self.add_error("password", "Password is required")
            else:
                self.add_error("password", "Password is required")

        return cleaned_data

    def save(self, commit=True):
        # Save existing data before super().save() modifies instance
        existing_data = (
            self.instance.data.copy() if self.instance.pk and self.instance.data else {}
        )

        instance = super().save(commit=False)

        # Explicitly set check_type to prevent tampering
        instance.check_type = CheckType.IMAP

        # Build URL from host and port
        host = self.cleaned_data["host"]
        port = self.cleaned_data["port"]
        instance.url = host

        # Populate data JSONField from form fields
        instance.data = {
            "host": host,
            "port": port,
            "tls_mode": self.cleaned_data["tls_mode"],
            "username": self.cleaned_data["username"],
            "folder": self.cleaned_data["folder"],
            "search_subject": self.cleaned_data["search_subject"],
            "max_age_minutes": self.cleaned_data["max_age_minutes"],
            "delete_after_check": self.cleaned_data["delete_after_check"],
            "timeout": self.cleaned_data["timeout"],
            "retries": self.cleaned_data["retries"],
            "retry_delay": self.cleaned_data["retry_delay"],
        }

        # Handle password - keep existing if not provided
        if self.cleaned_data.get("password"):
            instance.data["password"] = self.cleaned_data["password"]
        elif existing_data.get("password"):
            # Preserve existing password when editing
            instance.data["password"] = existing_data["password"]

        if commit:
            instance.save()

        return instance


class TcpHealthCheckForm(HealthCheckForm):
    """Form for TCP health checks.

    Adds TCP-specific fields and validation.
    Handles serialization to/from HealthCheck.data JSONField.
    """

    host = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "mail.example.com"}
        ),
        label="Host",
        help_text="Hostname or IP address to connect to",
    )

    port = forms.IntegerField(
        initial=443,
        min_value=1,
        max_value=65535,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Port",
        help_text="TCP port to connect to",
    )

    tls_mode = forms.ChoiceField(
        choices=[
            ("none", "None"),
            ("implicit", "Implicit TLS"),
            ("starttls", "STARTTLS"),
        ],
        initial="none",
        widget=forms.Select(attrs={"class": "form-control"}),
        label="TLS Mode",
        help_text="TLS negotiation mode; use STARTTLS for ports like 25/587",
    )

    connect_timeout = forms.FloatField(
        initial=10.0,
        min_value=1.0,
        max_value=60.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Connect Timeout (seconds)",
        help_text="Timeout for establishing the TCP connection",
    )

    tls_handshake_timeout = forms.FloatField(
        initial=10.0,
        min_value=1.0,
        max_value=60.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="TLS Handshake Timeout (seconds)",
        help_text="Timeout for TLS negotiation (when TLS is enabled)",
    )

    retries = forms.IntegerField(
        initial=1,
        min_value=0,
        max_value=10,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Retries",
        help_text="Number of retry attempts on failure",
    )

    retry_delay = forms.FloatField(
        initial=0.0,
        min_value=0.0,
        max_value=60.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Retry Delay (seconds)",
        help_text="Delay between retry attempts",
    )

    check_cert_expiry = forms.BooleanField(
        initial=False,
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Check Certificate Expiry",
        help_text="Validate TLS certificate expiry (TLS only)",
    )

    min_cert_days = forms.IntegerField(
        initial=14,
        min_value=0,
        max_value=365,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Min Certificate Days",
        help_text="Fail when certificate expires sooner than this",
    )

    sni = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "mail.example.com"}
        ),
        label="SNI Hostname",
        help_text="Optional SNI override for TLS connections",
    )

    verify = forms.BooleanField(
        initial=True,
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
        label="Verify TLS Certificates",
        help_text="Disable only for debugging",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # Force check_type to TCP
        self.fields["check_type"].initial = CheckType.TCP
        self.fields["check_type"].widget = forms.HiddenInput()

        # Hide URL field; host is the source of truth
        self.fields["url"].required = False
        self.fields["url"].widget = forms.HiddenInput()
        if self.instance.pk and self.instance.url:
            self.fields["url"].initial = self.instance.url

        if self.instance.pk and self.instance.data:
            tcp_config = self.instance.data
            self.fields["host"].initial = tcp_config.get("host") or self.instance.url
            self.fields["port"].initial = tcp_config.get("port", 443)
            self.fields["tls_mode"].initial = tcp_config.get("tls_mode", "none")
            self.fields["connect_timeout"].initial = tcp_config.get(
                "connect_timeout", 10.0
            )
            self.fields["tls_handshake_timeout"].initial = tcp_config.get(
                "tls_handshake_timeout", 10.0
            )
            self.fields["retries"].initial = tcp_config.get("retries", 1)
            self.fields["retry_delay"].initial = tcp_config.get("retry_delay", 0.0)
            self.fields["check_cert_expiry"].initial = tcp_config.get(
                "check_cert_expiry", False
            )
            self.fields["min_cert_days"].initial = tcp_config.get("min_cert_days", 14)
            self.fields["sni"].initial = tcp_config.get("sni", "")
            self.fields["verify"].initial = tcp_config.get("verify", True)

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        self.warnings: list[str] = []

        tls_mode = cleaned_data.get("tls_mode")
        port = cleaned_data.get("port")

        if tls_mode == "none":
            cleaned_data["check_cert_expiry"] = False

        if tls_mode == "starttls":
            self.warnings.append(
                "TCP STARTTLS is a generic probe that sends STARTTLS immediately. "
                "SMTP/IMAP servers usually require an EHLO/LOGIN exchange first. "
                "Use SMTP/IMAP checks for protocol validation, or TLS=None for reachability."
            )
            if port in {25, 587, 143}:
                self.warnings.append(
                    f"Port {port} typically expects full SMTP/IMAP STARTTLS negotiation; "
                    "this TCP check will likely fail even if the service is healthy."
                )

        return cleaned_data

    def save(self, commit: bool = True) -> HealthCheck:
        instance = super().save(commit=False)
        instance.check_type = CheckType.TCP

        host = self.cleaned_data["host"]
        instance.url = host

        data = {
            "host": host,
            "port": self.cleaned_data["port"],
            "tls_mode": self.cleaned_data["tls_mode"],
            "connect_timeout": self.cleaned_data["connect_timeout"],
            "tls_handshake_timeout": self.cleaned_data["tls_handshake_timeout"],
            "retries": self.cleaned_data["retries"],
            "retry_delay": self.cleaned_data["retry_delay"],
            "check_cert_expiry": self.cleaned_data["check_cert_expiry"],
            "min_cert_days": self.cleaned_data["min_cert_days"],
            "verify": self.cleaned_data["verify"],
        }

        if self.cleaned_data["tls_mode"] == "starttls":
            data["starttls_command"] = "STARTTLS\r\n"

        if self.cleaned_data.get("sni"):
            data["sni"] = self.cleaned_data["sni"]

        instance.data = data

        if commit:
            instance.save()
        return instance


class JsonMetricsHealthCheckForm(HealthCheckForm):
    """Form for JSON metrics health checks.

    Stores configuration in HealthCheck.data JSONField.
    """

    url = forms.CharField(
        max_length=512,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "http://host:9100/.well-known/health",
            }
        ),
        label="URL",
        help_text="JSON health endpoint URL",
    )

    auth_username = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "nyxmon"}),
        label="Auth Username",
        help_text="Optional HTTP Basic Auth username",
    )

    auth_password = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "form-control", "placeholder": "Leave blank to keep existing"}
        ),
        label="Auth Password",
        help_text="Optional HTTP Basic Auth password",
    )

    timeout = forms.FloatField(
        initial=10.0,
        min_value=1.0,
        max_value=120.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Timeout (seconds)",
        help_text="HTTP request timeout in seconds",
    )

    retries = forms.IntegerField(
        initial=1,
        min_value=0,
        max_value=10,
        widget=forms.NumberInput(attrs={"class": "form-control"}),
        label="Retries",
        help_text="Number of retry attempts on failure",
    )

    retry_delay = forms.FloatField(
        initial=2.0,
        min_value=0.0,
        max_value=60.0,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1"}),
        label="Retry Delay (seconds)",
        help_text="Delay between retry attempts",
    )

    checks_json = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 8,
                "placeholder": """[
  {"path": "$.mail.queue_total", "op": "<", "value": 100, "severity": "warning"},
  {"path": "$.mail.queue_total", "op": "<", "value": 500, "severity": "critical"}
]""",
            }
        ),
        label="Checks (JSON)",
        help_text="JSON array of threshold checks (path/op/value/severity).",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Force check_type to JSON_METRICS
        self.fields["check_type"].initial = CheckType.JSON_METRICS
        self.fields["check_type"].widget = forms.HiddenInput()

        if self.instance.pk and self.instance.data:
            cfg = self.instance.data
            self.fields["url"].initial = cfg.get("url", self.instance.url)
            auth = cfg.get("auth") or {}
            self.fields["auth_username"].initial = auth.get("username", "")
            # Don't populate password (keep existing)
            self.fields["timeout"].initial = cfg.get("timeout", 10.0)
            self.fields["retries"].initial = cfg.get("retries", 1)
            self.fields["retry_delay"].initial = cfg.get("retry_delay", 2.0)
            checks = cfg.get("checks", [])
            try:
                self.fields["checks_json"].initial = json.dumps(checks, indent=2)
            except Exception:
                self.fields["checks_json"].initial = "[]"

    def clean(self):
        cleaned_data = super().clean()
        self.warnings: list[str] = []

        raw_checks = cleaned_data.get("checks_json", "")
        try:
            checks = json.loads(raw_checks)
        except Exception as exc:
            self.add_error("checks_json", f"Invalid JSON: {exc}")
            return cleaned_data

        if not isinstance(checks, list):
            self.add_error("checks_json", "Checks must be a JSON array")
            return cleaned_data

        cleaned_data["_checks"] = checks

        username = cleaned_data.get("auth_username") or ""
        password = cleaned_data.get("auth_password") or ""
        if bool(username) != bool(password):
            if self.instance.pk and self.instance.data and username and not password:
                # allow "username set, keep existing password" on edit
                pass
            else:
                self.add_error(
                    "auth_password",
                    "Provide both username and password for Basic Auth",
                )

        return cleaned_data

    def save(self, commit=True):
        existing_data = (
            self.instance.data.copy() if self.instance.pk and self.instance.data else {}
        )
        instance = super().save(commit=False)
        instance.check_type = CheckType.JSON_METRICS

        url = self.cleaned_data["url"]
        instance.url = url

        data = {
            "url": url,
            "timeout": self.cleaned_data["timeout"],
            "retries": self.cleaned_data["retries"],
            "retry_delay": self.cleaned_data["retry_delay"],
            "checks": self.cleaned_data.get("_checks", []),
        }

        username = self.cleaned_data.get("auth_username") or ""
        password = self.cleaned_data.get("auth_password") or ""
        if username:
            data["auth"] = {"username": username}
            if password:
                data["auth"]["password"] = password
            else:
                existing_auth = (existing_data.get("auth") or {}) if existing_data else {}
                if existing_auth.get("username") == username and existing_auth.get("password"):
                    data["auth"]["password"] = existing_auth["password"]

        instance.data = data

        if commit:
            instance.save()
        return instance


class DnsHealthCheckForm(HealthCheckForm):
    """Form for DNS health checks.

    Adds DNS-specific fields and validation.
    Handles serialization to/from HealthCheck.data JSONField.
    """

    # DNS-specific fields (not in model, stored in data JSONField)
    expected_ips = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "class": "form-control",
                "placeholder": "192.168.1.100\n192.168.1.101",
            }
        ),
        required=True,  # Required for DNS checks
        label="Expected IP Addresses",
        help_text="Enter expected IP addresses, one per line (literal IPs only)",
    )

    dns_server = forms.GenericIPAddressField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "8.8.8.8"}
        ),
        label="DNS Server",
        help_text="DNS server to query (leave blank for system default)",
    )

    source_ip = forms.GenericIPAddressField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "192.168.1.50"}
        ),
        label="Source IP Address",
        help_text="Source IP address to bind for the query (for split-horizon DNS testing)",
    )

    query_type = forms.ChoiceField(
        choices=[
            ("A", "A (IPv4 Address)"),
            ("AAAA", "AAAA (IPv6 Address)"),
            # Future: MX, TXT, CNAME, NS, SOA, PTR
            # Requires record-type-specific matching logic
        ],
        initial="A",
        required=True,
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Query Type",
        help_text="Initial implementation supports A and AAAA records only",
    )

    timeout = forms.FloatField(
        required=True,  # Must not be None - causes validation crash
        initial=5.0,
        min_value=0.1,
        max_value=60.0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.1",
                # Note: Don't hard-code 'value' here - it breaks editing
                # Let the field's initial/bound data drive the value
            }
        ),
        label="Timeout (seconds)",
        help_text="Query timeout in seconds (default: 5.0)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Force check_type to DNS
        self.fields["check_type"].initial = CheckType.DNS
        self.fields["check_type"].widget = forms.HiddenInput()

        # Customize url field for DNS semantics
        self.fields["url"].label = "Domain"
        self.fields["url"].help_text = "Domain name to query (e.g., wersdörfer.de)"
        self.fields["url"].widget.attrs["placeholder"] = "example.com"

        # If editing existing DNS check, populate DNS fields from data
        if self.instance.pk and self.instance.data:
            dns_config = self.instance.data
            self.fields["expected_ips"].initial = "\n".join(
                dns_config.get("expected_ips", [])
            )
            self.fields["dns_server"].initial = dns_config.get("dns_server")
            self.fields["source_ip"].initial = dns_config.get("source_ip")
            self.fields["query_type"].initial = dns_config.get("query_type", "A")
            self.fields["timeout"].initial = dns_config.get("timeout", 5.0)

    def clean_expected_ips(self):
        """Parse and validate expected IPs.

        Returns:
            List of literal IP address strings

        Raises:
            ValidationError: If any IP is invalid

        Note:
            Only literal IP addresses supported. CIDR ranges and wildcards
            are not supported in initial implementation.
        """
        value = self.cleaned_data.get("expected_ips", "")
        if not value:
            raise forms.ValidationError(
                "Expected IP addresses are required for DNS checks"
            )

        # Split by newlines and filter empty lines
        ips = [line.strip() for line in value.split("\n") if line.strip()]

        # Validate each IP address (literal only, no CIDR/wildcards)
        validated_ips = []
        for ip in ips:
            try:
                # This will raise ValidationError if invalid
                forms.GenericIPAddressField().clean(ip)
                validated_ips.append(ip)
            except forms.ValidationError:
                raise forms.ValidationError(
                    f"Invalid IP address: {ip}. "
                    "Only literal IP addresses supported (no CIDR ranges or wildcards)."
                )

        return validated_ips

    def clean_query_type(self):
        """Validate query type is supported."""
        query_type = self.cleaned_data.get("query_type")
        if query_type not in ("A", "AAAA"):
            raise forms.ValidationError(
                f"Query type {query_type} not supported in initial implementation. "
                "Only A and AAAA records supported."
            )
        return query_type

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Explicitly set check_type to prevent tampering
        # Hidden field in form doesn't prevent crafted POST from changing it
        instance.check_type = CheckType.DNS

        # Populate data JSONField from DNS form fields
        instance.data = {
            "expected_ips": self.cleaned_data["expected_ips"],
            "query_type": self.cleaned_data["query_type"],
            "timeout": self.cleaned_data["timeout"],
        }

        # Add optional fields only if provided
        if self.cleaned_data.get("dns_server"):
            instance.data["dns_server"] = self.cleaned_data["dns_server"]

        if self.cleaned_data.get("source_ip"):
            instance.data["source_ip"] = self.cleaned_data["source_ip"]

        if commit:
            instance.save()

        return instance
