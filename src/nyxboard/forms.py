from django import forms
from django.core.validators import URLValidator

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
