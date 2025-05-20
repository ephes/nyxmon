from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json
from time import time

from .models import Service, HealthCheck, StatusChoices
from .forms import ServiceForm, HealthCheckForm
from nyxmon.domain import CheckStatus


def dashboard(request):
    """
    Function-based view to display the dashboard of services and their health checks.
    """
    # Get all services with their health checks
    services = Service.objects.prefetch_related("healthcheck_set")

    # Fetch recent results for all health checks separately
    health_checks = HealthCheck.objects.filter(service__in=services)

    # Dictionary to map check IDs to their last result
    check_results = {}

    # For each health check, fetch its recent results separately and determine mode
    current_time = time()

    for check in health_checks:
        # Get recent results
        recent_results = check.results.order_by("-created_at")[:5]
        check.recent_results = list(
            recent_results
        )  # Force evaluation and convert to list

        # Set check mode - determines if progress ring is shown or if it's due for a check
        if check.next_check_time <= current_time:
            check.check_mode = "due"
        else:
            check.check_mode = "normal"

        # Get last result if any
        if check.recent_results:
            check.last_result = check.recent_results[0]
            # Add to our mapping dictionary
            check_results[check.id] = {
                "formatted_time": check.last_result.created_at.strftime(
                    "%b %-d, %H:%M"
                ),
                "timestamp": int(check.last_result.created_at.timestamp()),
                "status": check.last_result.status,
            }
        else:
            check.last_result = None

    # Set the default theme if not in session
    if "theme" not in request.session:
        request.session["theme"] = "light"

    # Create a JSON object with only the essential check result data
    check_results_json = {}
    for check_id, result_data in check_results.items():
        check_results_json[str(check_id)] = {
            "formattedTime": result_data["formatted_time"]
        }

    context = {
        "services": services,
        "status_choices": StatusChoices,
        "theme": request.session.get("theme", "light"),
        "check_results_json": json.dumps(check_results_json),
        "check_results": check_results_json,
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
        # Check if this is a quick-toggle of the disabled flag from the card
        if (
            "disabled" in request.POST and len(request.POST) == 2
        ):  # Just disabled and csrf token
            new_disabled_value = request.POST.get("disabled") == "1"
            health_check.disabled = new_disabled_value
            health_check.save()

            # Redirect back to referring page, or dashboard if no referrer
            if request.META.get("HTTP_REFERER"):
                return redirect(request.META.get("HTTP_REFERER"))
            return redirect("nyxboard:dashboard")

        # Normal form submission
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

    # Set last_result regardless of status
    last_result = recent_results[0] if recent_results else None

    if health_check.status == CheckStatus.PROCESSING:
        # If it's being processed, keep in due mode
        check_mode = "due"
    elif health_check.next_check_time <= current_time:
        # If it's past next check time, it's still due
        check_mode = "due"
    else:
        # If next check time is in the future, it's back to normal
        check_mode = "normal"

    # Determine which template to use based on what partial was requested
    template_name = "nyxboard/partials/healthcheck-card.html"
    if "healthcheck.html" in request.headers.get("HX-Request-URL", ""):
        template_name = "nyxboard/partials/healthcheck.html"

    context = {
        "check": health_check,
        "check_mode": check_mode,
        "last_result": last_result,
        "theme": request.session.get("theme", "light"),
    }

    return render(request, template_name, context)


def healthcheck_trigger(request, check_id):
    """
    Manually trigger a health check to be run now.
    This marks the check as due immediately.
    """
    if request.method != "POST":
        return redirect("nyxboard:dashboard")

    health_check = get_object_or_404(HealthCheck, id=check_id)

    # Get data needed for the template first
    recent_results = health_check.results.order_by("-created_at")[:5]
    last_result = recent_results[0] if recent_results else None
    health_check.recent_results = recent_results

    # Set the next check time to now, so it will be picked up by the agent
    health_check.next_check_time = int(time())
    health_check.save()

    # Determine which template to use based on what partial was requested
    template_name = "nyxboard/partials/healthcheck-card.html"
    if "healthcheck.html" in request.headers.get("HX-Request-URL", ""):
        template_name = "nyxboard/partials/healthcheck.html"

    context = {
        "check": health_check,
        "check_mode": "due",
        "last_result": last_result,  # Use the stored last_result variable
        "theme": request.session.get("theme", "light"),
    }

    return render(request, template_name, context)


def healthcheck_toggle_disabled(request, check_id):
    """
    Toggle the disabled status of a health check.
    Uses HTMX to update the check card.
    """
    if request.method != "POST":
        return redirect("nyxboard:dashboard")

    health_check = get_object_or_404(HealthCheck, id=check_id)

    # Get data needed for the template first
    recent_results = health_check.results.order_by("-created_at")[:5]
    last_result = recent_results[0] if recent_results else None
    health_check.recent_results = recent_results

    # Toggle the disabled status
    health_check.disabled = not health_check.disabled

    # If we're disabling, reset the next check time to now
    # This prevents the progress ring from showing progress for disabled checks
    if health_check.disabled:
        health_check.next_check_time = int(time())

    health_check.save()

    # Determine check mode based on current status
    current_time = time()
    if health_check.disabled:
        check_mode = "normal"  # Don't show due status for disabled checks
    elif health_check.next_check_time <= current_time:
        check_mode = "due"
    else:
        check_mode = "normal"

    # Determine which template to use based on what partial was requested
    template_name = "nyxboard/partials/healthcheck-card.html"
    if "healthcheck.html" in request.headers.get("HX-Request-URL", ""):
        template_name = "nyxboard/partials/healthcheck.html"

    context = {
        "check": health_check,
        "check_mode": check_mode,
        "last_result": last_result,
        "theme": request.session.get("theme", "light"),
    }

    return render(request, template_name, context)


@require_POST
def set_theme(request):
    """
    Set the theme preference in the session.
    """
    try:
        data = json.loads(request.body)
        theme = data.get("theme", "light")
        request.session["theme"] = theme
        return JsonResponse({"status": "success", "theme": theme})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
