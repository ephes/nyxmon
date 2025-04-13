from django.db import models


class StatusChoices:
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    RECOVERING = "recovering"
    UNKNOWN = "unknown"

    CHOICES = [
        (PASSED, "Passed"),
        (FAILED, "Failed"),
        (WARNING, "Warning"),
        (RECOVERING, "Recovering"),
        (UNKNOWN, "Unknown"),
    ]

    @classmethod
    def get_css_class(cls, status):
        css_classes = {
            cls.PASSED: "status-passed",
            cls.FAILED: "status-failed",
            cls.WARNING: "status-warning",
            cls.RECOVERING: "status-recovering",
            cls.UNKNOWN: "status-unknown",
        }
        return css_classes.get(status, "")


class Service(models.Model):
    data = models.JSONField("Metadata", blank=True, default=dict)

    def __str__(self):
        return f"Service {self.id}"

    def get_status(self):
        """
        Calculate the overall status of the service based on its health checks.
        """
        health_checks = self.healthcheck_set.all()

        if not health_checks.exists():
            return StatusChoices.UNKNOWN

        statuses = [check.get_status() for check in health_checks]

        if StatusChoices.FAILED in statuses:
            return StatusChoices.FAILED

        if StatusChoices.WARNING in statuses or StatusChoices.RECOVERING in statuses:
            return StatusChoices.WARNING

        if all(status == StatusChoices.PASSED for status in statuses):
            return StatusChoices.PASSED

        if all(status == StatusChoices.UNKNOWN for status in statuses):
            return StatusChoices.UNKNOWN

        return StatusChoices.WARNING


class HealthCheck(models.Model):
    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    data = models.JSONField("Metadata", blank=True, default=dict)

    def __str__(self):
        return f"Check {self.id} for {self.service}"

    def get_status(self):
        """
        Calculate the health check status based on recent results.
        """
        # Get the 5 most recent results
        recent_results = self.results.order_by("-created_at")[:5]

        if not recent_results.exists():
            return StatusChoices.UNKNOWN

        latest_result = recent_results.first()

        # Check for Failed status (latest result is error)
        if latest_result.status == "error":
            return StatusChoices.FAILED

        # Check for Passed status (all recent results are OK)
        if all(result.status == "ok" for result in recent_results):
            return StatusChoices.PASSED

        # Check for Recovering status (latest is OK but there were recent errors)
        if latest_result.status == "ok" and any(
            result.status == "error" for result in recent_results
        ):
            return StatusChoices.RECOVERING

        # Otherwise, it's a Warning status
        return StatusChoices.WARNING


class Result(models.Model):
    health_check = models.ForeignKey(
        HealthCheck, on_delete=models.CASCADE, related_name="results"
    )
    status = models.CharField(
        "Status", max_length=10, choices=[("ok", "OK"), ("error", "Error")]
    )
    created_at = models.DateTimeField("Created At", auto_now_add=True)
    data = models.JSONField("Metadata", blank=True, default=dict)

    def __str__(self):
        return f"Result {self.id} for {self.health_check} ({self.status})"
