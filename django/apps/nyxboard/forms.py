from django import forms
from django.core.exceptions import ValidationError
import json

from .models import Service, HealthCheck


class ServiceForm(forms.ModelForm):
    """Form for creating and updating Service objects."""

    class Meta:
        model = Service
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"class": "form-control"})}


class HealthCheckForm(forms.ModelForm):
    """Form for creating and updating HealthCheck objects."""

    data_json = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}),
        required=False,
        label="Health Check Data (JSON)",
        help_text="Enter the health check metadata as a JSON object.",
    )

    class Meta:
        model = HealthCheck
        fields = ["service"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            # If editing an existing instance, populate the data_json field
            self.fields["data_json"].initial = json.dumps(self.instance.data, indent=2)

    def clean_data_json(self):
        data_json = self.cleaned_data.get("data_json", "{}")
        if not data_json:
            return {}

        try:
            return json.loads(data_json)
        except json.JSONDecodeError:
            raise ValidationError("Invalid JSON format. Please check your input.")

    def save(self, commit=True):
        health_check = super().save(commit=False)
        health_check.data = self.cleaned_data.get("data_json", {})

        if commit:
            health_check.save()

        return health_check
