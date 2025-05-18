from django.shortcuts import render, redirect, get_object_or_404
from time import time

from .models import Service, HealthCheck, StatusChoices
from .forms import ServiceForm, HealthCheckForm


def dashboard(request):
    """
    Function-based view to display the dashboard of services and their health checks.
    """
    # Get all services with their health checks
    services = Service.objects.prefetch_related("healthcheck_set")

    # Fetch recent results for all health checks separately
    health_checks = HealthCheck.objects.filter(service__in=services)

    # For each health check, fetch its recent results separately and determine mode
    current_time = time()

    for check in health_checks:
        check.recent_results = check.results.order_by("-created_at")[:5]

        # Set check mode - determines if progress ring is shown or if it's due for a check
        if check.next_check_time <= current_time:
            check.check_mode = "due"
        else:
            check.check_mode = "normal"

        # Get last result if any
        if check.recent_results:
            check.last_result = check.recent_results[0]
        else:
            check.last_result = None

        # Make sure we're printing out check.check_mode to debug
        print(f"Check {check.id}: {check.name} - Mode: {check.check_mode}")

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
            # Check if check_interval has changed
            if "check_interval" in form.changed_data:
                # Reset next_check_time to now to make the check due immediately
                health_check = form.save(commit=False)
                health_check.next_check_time = int(time())
                health_check.save()
            else:
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


# HTMX-enabled views for health check updates
def healthcheck_update_status(request, check_id):
    """
    Update the status of a health check for HTMX updates.
    This view is called periodically to check if a health check's status has changed.
    """
    health_check = get_object_or_404(HealthCheck, id=check_id)
    recent_results = health_check.results.order_by("-created_at")[:5]

    # Attach needed data to the health check for the template
    health_check.recent_results = recent_results

    # Determine if it's still due or back to normal
    current_time = time()

    if health_check.status == "processing":
        # If it's being processed, keep in due mode
        check_mode = "due"
        last_result = None
    elif health_check.next_check_time <= current_time:
        # If it's past next check time, it's still due
        check_mode = "due"
        last_result = recent_results[0] if recent_results else None
    else:
        # If next check time is in the future, it's back to normal
        check_mode = "normal"
        last_result = recent_results[0] if recent_results else None

    # Debug output
    print(f"HTMX Update - Check {check_id}: {health_check.name} - Mode: {check_mode}")

    context = {
        "check": health_check,
        "check_mode": check_mode,
        "last_result": last_result,
    }

    return render(request, "nyxboard/partials/healthcheck.html", context)


def healthcheck_trigger(request, check_id):
    """
    Manually trigger a health check to be run now.
    This marks the check as due immediately.
    """
    if request.method != "POST":
        return redirect("nyxboard:dashboard")

    health_check = get_object_or_404(HealthCheck, id=check_id)

    # Set the next check time to now, so it will be picked up by the agent
    health_check.next_check_time = int(time())
    health_check.save()

    # Get data needed for the template
    recent_results = health_check.results.order_by("-created_at")[:5]
    health_check.recent_results = recent_results

    # Debug output
    print(f"HTMX Trigger - Check {check_id}: {health_check.name} - Mode: due")

    context = {
        "check": health_check,
        "check_mode": "due",
        "last_result": recent_results[0] if recent_results else None,
    }

    return render(request, "nyxboard/partials/healthcheck.html", context)
