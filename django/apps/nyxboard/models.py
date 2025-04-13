from django.db import models


class Service(models.Model):
    data = models.JSONField("Metadata", blank=True, default=dict)


class HealthCheck(models.Model):
    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    data = models.JSONField("Metadata", blank=True, default=dict)


class Result(models.Model):
    health_check = models.ForeignKey(
        HealthCheck, on_delete=models.CASCADE, related_name="results"
    )
    status = models.CharField(
        "Status", max_length=10, choices=[("ok", "OK"), ("error", "Error")]
    )
    created_at = models.DateTimeField("Created At", auto_now_add=True)
    data = models.JSONField("Metadata", blank=True, default=dict)
