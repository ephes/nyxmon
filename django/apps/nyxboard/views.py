from django.shortcuts import render
from django.db.models import Prefetch

from .models import Service, HealthCheck, Result, StatusChoices


def dashboard(request):
    """
    Function-based view to display the dashboard of services and their health checks.
    """
    # Optimize the query with prefetch_related to reduce database hits
    # Prefetch recent results for each health check
    results_prefetch = Prefetch(
        "results",
        queryset=Result.objects.order_by("-created_at")[:5],
    )

    # Prefetch health checks with their recent results
    health_checks_prefetch = Prefetch(
        "healthcheck_set",
        queryset=HealthCheck.objects.prefetch_related(results_prefetch),
    )

    # Get all services with prefetched data
    services = Service.objects.prefetch_related(health_checks_prefetch)

    context = {
        "services": services,
        "status_choices": StatusChoices,
    }

    return render(request, "nyxboard/dashboard.html", context)
