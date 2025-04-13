from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Prefetch

from .models import Service, HealthCheck, Result, StatusChoices
from .forms import ServiceForm, HealthCheckForm


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


# Service CRUD views
def service_list(request):
    """
    Display a list of all services.
    """
    services = Service.objects.all()
    return render(request, "nyxboard/service_list.html", {"services": services})


def service_detail(request, service_id):
    """
    Display details of a specific service.
    """
    service = get_object_or_404(Service, id=service_id)
    health_checks = service.healthcheck_set.all()
    return render(
        request,
        "nyxboard/service_detail.html",
        {"service": service, "health_checks": health_checks},
    )


def service_create(request):
    """
    Create a new service.
    """
    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            service = form.save()
            return redirect("nyxboard:service_detail", service_id=service.id)
    else:
        form = ServiceForm()

    return render(
        request, "nyxboard/service_form.html", {"form": form, "action": "Create"}
    )


def service_update(request, service_id):
    """
    Update an existing service.
    """
    service = get_object_or_404(Service, id=service_id)

    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            return redirect("nyxboard:service_detail", service_id=service.id)
    else:
        form = ServiceForm(instance=service)

    return render(
        request,
        "nyxboard/service_form.html",
        {"form": form, "service": service, "action": "Update"},
    )


def service_delete(request, service_id):
    """
    Delete a service.
    """
    service = get_object_or_404(Service, id=service_id)

    if request.method == "POST":
        service.delete()
        return redirect("nyxboard:service_list")

    return render(request, "nyxboard/service_confirm_delete.html", {"service": service})


# HealthCheck CRUD views
def healthcheck_list(request):
    """
    Display a list of all health checks.
    """
    health_checks = HealthCheck.objects.all()
    return render(
        request, "nyxboard/healthcheck_list.html", {"health_checks": health_checks}
    )


def healthcheck_detail(request, check_id):
    """
    Display details of a specific health check.
    """
    health_check = get_object_or_404(HealthCheck, id=check_id)
    results = health_check.results.order_by("-created_at")[:10]
    return render(
        request,
        "nyxboard/healthcheck_detail.html",
        {"health_check": health_check, "results": results},
    )


def healthcheck_create(request, service_id=None):
    """
    Create a new health check, optionally linked to a specific service.
    """
    initial = {}
    service = None

    if service_id:
        service = get_object_or_404(Service, id=service_id)
        initial["service"] = service

    if request.method == "POST":
        form = HealthCheckForm(request.POST)
        if form.is_valid():
            health_check = form.save()
            return redirect("nyxboard:healthcheck_detail", check_id=health_check.id)
    else:
        form = HealthCheckForm(initial=initial)

    return render(
        request,
        "nyxboard/healthcheck_form.html",
        {"form": form, "service": service, "action": "Create"},
    )


def healthcheck_update(request, check_id):
    """
    Update an existing health check.
    """
    health_check = get_object_or_404(HealthCheck, id=check_id)

    if request.method == "POST":
        form = HealthCheckForm(request.POST, instance=health_check)
        if form.is_valid():
            form.save()
            return redirect("nyxboard:healthcheck_detail", check_id=health_check.id)
    else:
        form = HealthCheckForm(instance=health_check)

    return render(
        request,
        "nyxboard/healthcheck_form.html",
        {"form": form, "health_check": health_check, "action": "Update"},
    )


def healthcheck_delete(request, check_id):
    """
    Delete a health check.
    """
    health_check = get_object_or_404(HealthCheck, id=check_id)

    if request.method == "POST":
        service_id = health_check.service.id
        health_check.delete()
        return redirect("nyxboard:service_detail", service_id=service_id)

    return render(
        request,
        "nyxboard/healthcheck_confirm_delete.html",
        {"health_check": health_check},
    )
