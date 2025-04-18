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
    name = models.CharField("Service Name", max_length=255)

    def __str__(self):
        return self.name

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
    CHECK_TYPE_CHOICES = [
        ("http", "HTTP"),
        ("tcp", "TCP"),
        ("ping", "Ping"),
        ("dns", "DNS"),
        ("custom", "Custom"),
    ]

    INTERVAL_CHOICES = [
        (30, "30 seconds"),
        (60, "1 minute"),
        (300, "5 minutes"),
        (900, "15 minutes"),
        (1800, "30 minutes"),
        (3600, "1 hour"),
        (86400, "1 day"),
    ]

    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    check_type = models.CharField(
        "Check Type", max_length=20, choices=CHECK_TYPE_CHOICES, default="http"
    )
    url = models.URLField("URL", max_length=512)
    check_interval = models.IntegerField(
        "Check Interval",
        choices=INTERVAL_CHOICES,
        default=300,
        help_text="How often this health check should be performed",
    )

    def __str__(self):
        return f"{self.get_check_type_display()} Check ({self.id}) for {self.service}"

    def get_status(self):
        """
        Calculate the health check status based on recent results.
        """
        # Use recent_results if it's available (set by the dashboard view)
        # Otherwise, query the database
        if hasattr(self, "recent_results"):
            recent_results = self.recent_results
        else:
            # Get the 5 most recent results
            recent_results = self.results.order_by("-created_at")[:5]

        if not recent_results:
            return StatusChoices.UNKNOWN

        latest_result = recent_results[0] if recent_results else None

        if not latest_result:
            return StatusChoices.UNKNOWN

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
