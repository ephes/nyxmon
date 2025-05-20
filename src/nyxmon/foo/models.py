from time import time

from django.db import models

from nyxmon.domain import ResultStatus, StatusChoices, CheckStatus, CheckType


class Service(models.Model):
    name: models.CharField = models.CharField("Service Name", max_length=255)

    class Meta:
        db_table = "service"

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
        (CheckType.HTTP, "HTTP"),
        (CheckType.JSON_HTTP, "JSON-HTTP"),
        (CheckType.TCP, "TCP"),
        (CheckType.PING, "Ping"),
        (CheckType.DNS, "DNS"),
        (CheckType.CUSTOM, "Custom"),
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

    STATUS_CHOICES = [
        (CheckStatus.IDLE, "Idle"),
        (CheckStatus.PROCESSING, "Processing"),
    ]

    name: models.CharField = models.CharField(
        "Check Name",
        max_length=255,
        help_text="A descriptive name for this health check",
    )
    service: models.ForeignKey = models.ForeignKey(Service, on_delete=models.CASCADE)
    check_type: models.CharField = models.CharField(
        "Check Type", max_length=20, choices=CHECK_TYPE_CHOICES, default="http"
    )
    url: models.URLField = models.URLField("URL", max_length=512)
    check_interval: models.IntegerField = models.IntegerField(
        "Check Interval",
        choices=INTERVAL_CHOICES,
        default=300,
        help_text="How often this health check should be performed",
    )
    status: models.CharField = models.CharField(
        "Status",
        max_length=10,
        choices=STATUS_CHOICES,
        default=CheckStatus.IDLE,
    )
    next_check_time: models.PositiveIntegerField = models.PositiveIntegerField(
        "Next Check Time", default=0, help_text="Unix timestamp of the next check"
    )
    processing_started_at: models.PositiveIntegerField = models.PositiveIntegerField(
        "Processing Started At",
        default=0,
        help_text="Unix timestamp when the check started processing",
    )
    disabled: models.BooleanField = models.BooleanField(
        "Disabled",
        default=False,
        help_text="If checked, this health check will not be executed",
    )

    class Meta:
        db_table = "health_check"

    def __str__(self):
        return f"{self.name} ({self.get_check_type_display()} Check {self.id})"

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

        # Check for Failed status (the latest result is error)
        if latest_result.status == ResultStatus.ERROR:
            return StatusChoices.FAILED

        # Check for Passed status (all recent results are OK)
        if all(result.status == ResultStatus.OK for result in recent_results):
            return StatusChoices.PASSED

        # Check for Recovering status (latest is OK but there were recent errors)
        if latest_result.status == ResultStatus.OK and any(
            result.status == ResultStatus.ERROR for result in recent_results
        ):
            return StatusChoices.RECOVERING

        # Otherwise, it's a Warning status
        return StatusChoices.WARNING

    @property
    def percentage_until_next_check(self):
        """
        Calculate the percentage of time until the next check.
        """
        if self.check_interval == 0:
            return 0
        current_time = time()
        return 100 - round(
            (self.next_check_time - current_time) / self.check_interval * 100
        )


class Result(models.Model):
    health_check: models.ForeignKey = models.ForeignKey(
        HealthCheck, on_delete=models.CASCADE, related_name="results"
    )
    status: models.CharField = models.CharField(
        "Status",
        max_length=10,
        choices=[(ResultStatus.OK, "OK"), (ResultStatus.ERROR, "Error")],
    )
    created_at: models.DateTimeField = models.DateTimeField(
        "Created At", auto_now_add=True
    )
    data = models.JSONField("Metadata", blank=True, default=dict)

    class Meta:
        db_table = "check_result"

    def __str__(self):
        return f"Result {self.id} for {self.health_check} ({self.status})"
