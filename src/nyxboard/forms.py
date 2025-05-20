from django import forms

from .models import Service, HealthCheck


class ServiceForm(forms.ModelForm):
    """Form for creating and updating Service objects."""

    class Meta:
        model = Service
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"class": "form-control"})}


class HealthCheckForm(forms.ModelForm):
    """Form for creating and updating HealthCheck objects."""

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
            "url": forms.URLInput(
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
