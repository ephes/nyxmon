"""
Development data utilities for creating test services and checks.
"""

from typing import Dict, List

from django.utils import timezone

from nyxmon.domain import CheckStatus
from nyxboard.models import Service, HealthCheck


def create_dev_service(name: str, *, using: str | None = None) -> Service:
    """Create a development service with the given name."""
    service = Service.objects.db_manager(using).create(name=name)
    return service


def create_dev_check(
    service: Service,
    name: str,
    check_type: str = "http",
    url: str = "http://localhost:8000/",
    interval: int = 60,
    disabled: bool = False,
    *,
    using: str | None = None,
) -> HealthCheck:
    """Create a development health check for the given service."""
    next_check_time = int(timezone.now().timestamp()) + interval

    # With a pinned writer, assigning the ID avoids routing an unsaved check
    # through Django's foreign key descriptor before the explicit insert.
    service_fields = (
        {"service": service} if using is None else {"service_id": service.pk}
    )
    check = HealthCheck.objects.db_manager(using).create(
        **service_fields,
        name=name,
        check_type=check_type,
        url=url,
        check_interval=interval,
        status=CheckStatus.IDLE,
        next_check_time=next_check_time,
        disabled=disabled,
    )

    return check


def create_sample_data(*, using: str | None = None) -> Dict[str, List]:
    """Create sample development data including services and checks."""
    # Track created objects
    created: dict[str, list] = {
        "services": [],
        "checks": [],
    }

    # Create Dev Service
    dev_service = create_dev_service("Development Server", using=using)
    created["services"].append(dev_service)

    # Create working check - checks the dashboard
    working_check = create_dev_check(
        service=dev_service,
        name="Dashboard Check",
        url="http://localhost:8000/",
        interval=60,
        using=using,
    )
    created["checks"].append(working_check)

    # Create failing check - non-existent URL
    failing_check = create_dev_check(
        service=dev_service,
        name="Failing Check",
        url="http://localhost:8000/non-existent-url/",
        interval=60,
        using=using,
    )
    created["checks"].append(failing_check)

    return created
