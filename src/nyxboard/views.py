from django.db.models import Case, F, PositiveIntegerField, Prefetch, When
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
import json
from datetime import datetime, timezone
from time import time

from .auth import nyxboard_login_required
from .deletion import DELETE_CONFLICT, delete_confirmed, deletion_revision
from .timestamps import usable_epoch as _usable_epoch
from .models import (
    CheckNotificationState,
    CollectorIncident,
    Service,
    HealthCheck,
    Result,
    StatusChoices,
)
from .forms import (
    DefinitionConflict,
    EDIT_CONFLICT,
    SERVICE_EDIT_CONFLICT,
    ServiceForm,
    HttpHealthCheckForm,
    DnsHealthCheckForm,
    SmtpHealthCheckForm,
    ImapHealthCheckForm,
    TcpHealthCheckForm,
    PingHealthCheckForm,
    JsonMetricsHealthCheckForm,
    GenericHealthCheckForm,
)
from nyxmon.domain import CheckStatus, CheckType

#: The single ``collector_incident`` row carrying the site connectivity
#: lifecycle. Mirrors ``nyxmon.adapters.site_connectivity.SITE_INCIDENT_KEY``;
#: kept as a literal so the dashboard does not import the agent's adapters.
SITE_INCIDENT_KEY = "site:connectivity"

#: Path states that make a path worth naming in the dashboard banner.
SITE_BANNER_STATES = ("down", "recovering")

#: How old ``observed_at`` may be before the banner stops presenting the payload
#: as a current observation. The worker distrusts its own snapshot after three
#: probe intervals; the dashboard uses a laxer ten minutes, so a single slow or
#: skipped round does not make the label flap.
SITE_OBSERVATION_STALE_SECONDS = 600

# Form class registry for per-type forms
FORM_CLASSES = {
    CheckType.HTTP: HttpHealthCheckForm,
    CheckType.JSON_HTTP: HttpHealthCheckForm,  # Reuse for now
    CheckType.DNS: DnsHealthCheckForm,
    CheckType.TCP: TcpHealthCheckForm,
    CheckType.PING: PingHealthCheckForm,
    CheckType.SMTP: SmtpHealthCheckForm,
    CheckType.IMAP: ImapHealthCheckForm,
    CheckType.JSON_METRICS: JsonMetricsHealthCheckForm,
}


def _format_epoch(value):
    """Render a Unix timestamp as a readable UTC string, or ``None``.

    Args:
        value: Anything a payload might carry in a timestamp field.

    Returns:
        ``"%b %-d, %H:%M UTC"`` for a usable positive epoch, otherwise ``None``.
    """
    epoch = _usable_epoch(value)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%b %-d, %H:%M UTC")


def site_connectivity_banner():
    """Build the dashboard banner for the site connectivity incident.

    Reads the single ``collector_incident`` row the observer maintains. A
    banner is shown when the row's payload says an outage is active, or when it
    still carries recovery summaries that have not been delivered yet.

    The banner reports what the observer measured, never what the worker does
    about it: the dashboard cannot see the worker's mode, so whether dependent
    alerts are actually held is stated as the condition it is.

    Returns:
        A context dict with ``paths`` (name, state and formatted ``down_since``
        of every path that is down or recovering), ``active``,
        ``summary_pending``, ``incident_id``, the formatted ``observed_at`` of
        the last probe round and ``stale`` (the round is more than
        :data:`SITE_OBSERVATION_STALE_SECONDS` old, in the future, or its
        timestamp is unusable, so the observer may be stopped or clocks skewed);
        or ``None`` when there is no row, nothing to report, or the payload is
        malformed. Nothing in here raises: a payload the dashboard cannot read simply produces no banner.
    """
    try:
        incident = CollectorIncident.objects.filter(
            incident_key=SITE_INCIDENT_KEY
        ).first()
    except Exception:
        return None
    if incident is None:
        return None
    payload = incident.payload
    if not isinstance(payload, dict):
        return None

    active = payload.get("phase") == "active"
    raw_summaries = payload.get("summaries")
    summary_pending = bool(raw_summaries) and isinstance(raw_summaries, list)
    if not active and not summary_pending:
        return None

    paths = []
    raw_paths = payload.get("paths")
    if isinstance(raw_paths, dict):
        for name, values in sorted(raw_paths.items()):
            if not isinstance(values, dict):
                continue
            state = values.get("state")
            if state not in SITE_BANNER_STATES:
                continue
            paths.append(
                {
                    "name": str(name),
                    "state": state,
                    "down_since": _format_epoch(values.get("down_since")),
                }
            )

    incident_id = payload.get("incident_id")
    observed_at = payload.get("observed_at")
    # Validate before arithmetic, retaining subsecond precision for clock skew.
    stale = _usable_epoch(observed_at) is None or not (
        0 <= time() - observed_at <= SITE_OBSERVATION_STALE_SECONDS
    )
    return {
        "active": active,
        "summary_pending": summary_pending,
        "incident_id": incident_id if isinstance(incident_id, int) else None,
        "paths": paths,
        "observed_at": _format_epoch(observed_at),
        "stale": stale,
    }


def _site_connectivity_badge(result):
    """Return ``"held"``, ``"observed"`` or ``None`` for one stored result.

    A result whose ``data`` carries ``site_connectivity`` was evaluated against
    the site state. ``held: true`` means its notification was deferred;
    anything else means the judgement was only recorded, which is what
    ``observe`` mode and an exhausted hold look like.
    """
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return None
    metadata = data.get("site_connectivity")
    if not isinstance(metadata, dict):
        return None
    return "held" if metadata.get("held") is True else "observed"


def _checks_with_recent_results():
    # A window per check fetches at most five samples, including timestamp ties.
    return HealthCheck.objects.prefetch_related(
        Prefetch(
            "results",
            queryset=Result.objects.order_by("-created_at", "-id")[:5],
            to_attr="recent_results",
        )
    )


def _services_with_recent_results():
    return Service.objects.prefetch_related(
        Prefetch("healthcheck_set", queryset=_checks_with_recent_results())
    )


def _project_service_statuses(services):
    for service in services:
        for check in service.healthcheck_set.all():
            check.dashboard_status = check.get_status()
        service.dashboard_status = service.get_status()


@nyxboard_login_required
def dashboard(request):
    """Display services and checks using bounded request-local evidence."""
    services = _services_with_recent_results()
    _project_service_statuses(services)

    # Dictionary to map check IDs to their last result
    check_results = {}

    current_time = time()
    for service in services:
        for check in service.healthcheck_set.all():
            check.check_mode = (
                "due" if check.next_check_time <= current_time else "normal"
            )
            check.last_result = (
                check.recent_results[0] if check.recent_results else None
            )
            if check.last_result:
                check_results[check.id] = {
                    "formatted_time": check.last_result.created_at.strftime(
                        "%b %-d, %H:%M"
                    ),
                    "timestamp": int(check.last_result.created_at.timestamp()),
                    "status": check.last_result.status,
                }

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
        "site_connectivity": site_connectivity_banner(),
    }

    return render(request, "nyxboard/dashboard.html", context)


# Service CRUD views
@nyxboard_login_required
def service_list(request):
    """
    Display a list of all services.
    """
    services = _services_with_recent_results()
    _project_service_statuses(services)
    return render(request, "nyxboard/service_list.html", {"services": services})


@nyxboard_login_required
def service_detail(request, service_id):
    """
    Display details of a specific service.
    """
    service = get_object_or_404(_services_with_recent_results(), id=service_id)
    _project_service_statuses([service])
    health_checks = service.healthcheck_set.all()
    return render(
        request,
        "nyxboard/service_detail.html",
        {"service": service, "health_checks": health_checks},
    )


@nyxboard_login_required
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
        request,
        "nyxboard/service_form.html",
        {
            "form": form,
            "action": "Create",
            "form_title": "Service",
            "back_url": reverse("nyxboard:service_list"),
        },
    )


@nyxboard_login_required
def service_update(request, service_id):
    """
    Update an existing service.
    """
    service = Service.objects.filter(id=service_id).first()
    deleted = service is None
    if deleted:
        if request.method != "POST" or "name" not in request.POST:
            get_object_or_404(Service, id=service_id)
        service = Service(id=service_id)
    response_status = 200
    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service, require_revision=True)
        valid = form.is_valid()
        if deleted:
            form.add_error(None, SERVICE_EDIT_CONFLICT)
            response_status = 409
        elif form.has_error("definition_revision", "conflict"):
            response_status = 409
        elif valid:
            try:
                form.save()
            except DefinitionConflict:
                form.add_error(None, SERVICE_EDIT_CONFLICT)
                response_status = 409
            else:
                return redirect("nyxboard:service_detail", service_id=service.id)
    else:
        form = ServiceForm(instance=service, require_revision=True)

    return render(
        request,
        "nyxboard/service_form.html",
        {
            "form": form,
            "service": service,
            "action": "Update",
            "form_title": "Service",
            "back_url": reverse("nyxboard:service_detail", args=[service.id]),
        },
        status=response_status,
    )


@nyxboard_login_required
def service_delete(request, service_id):
    """
    Delete a service.
    """
    conflict = False
    if request.method == "POST":
        service, deleted = delete_confirmed(
            Service, service_id, request.POST.get("definition_revision")
        )
        if deleted:
            return redirect("nyxboard:service_list")
        conflict = True
    else:
        service = get_object_or_404(Service, id=service_id)
    return render(
        request,
        "nyxboard/service_confirm_delete.html",
        {
            "service": service,
            "definition_revision": deletion_revision(service),
            "conflict": DELETE_CONFLICT if conflict else None,
        },
        status=409 if conflict else 200,
    )


# HealthCheck CRUD views
@nyxboard_login_required
def healthcheck_list(request):
    """
    Display a list of all health checks.
    """
    health_checks = _checks_with_recent_results().select_related("service")
    return render(
        request, "nyxboard/healthcheck_list.html", {"health_checks": health_checks}
    )


@nyxboard_login_required
def healthcheck_detail(request, check_id):
    """
    Display details of a specific health check.
    """
    health_check = get_object_or_404(HealthCheck, id=check_id)
    # The worker stamps results with second resolution, so two samples of one
    # check regularly share a ``created_at``; the descending id breaks that tie
    # in insertion order and keeps the truly newest sample first.
    results = list(health_check.results.order_by("-created_at", "-id")[:10])
    for result in results:
        result.site_connectivity_badge = _site_connectivity_badge(result)

    # ``held_since`` lives in the internal notification state table, which has
    # no row at all for a check that has never failed.
    notification_state = CheckNotificationState.objects.filter(
        health_check=health_check
    ).first()
    held_since = getattr(notification_state, "held_since", 0) or 0

    # ``held_since`` alone does not mean the alert is held: it stays non-zero
    # after the hold budget is exhausted and after a stale-snapshot bypass, and
    # it is deliberately carried through a maintenance-suppressed sample. Only
    # the newest stored sample knows whether its own notification was deferred.
    alert_held = bool(results) and results[0].site_connectivity_badge == "held"

    return render(
        request,
        "nyxboard/healthcheck_detail.html",
        {
            "health_check": health_check,
            "results": results,
            "held_since": _format_epoch(held_since),
            "alert_held": alert_held,
        },
    )


@nyxboard_login_required
def healthcheck_create(request, service_id=None):
    """
    Create a new health check, optionally linked to a specific service.
    """
    initial = {}
    service = None

    if service_id:
        service = get_object_or_404(Service, id=service_id)
        initial["service"] = service

    # Get check type from query parameter, default to HTTP
    check_type = request.GET.get("type", CheckType.HTTP)
    # Use generic form as fallback to preserve check_type and data for unmapped types (legacy types)
    FormClass = FORM_CLASSES.get(check_type, GenericHealthCheckForm)

    # Pass check_type in initial data to preserve it (important for JSON-HTTP and unmapped types)
    initial["check_type"] = check_type

    if request.method == "POST":
        form = FormClass(request.POST)
        if form.is_valid():
            health_check = form.save()
            return redirect("nyxboard:healthcheck_detail", check_id=health_check.id)
    else:
        form = FormClass(initial=initial)

    # Select template based on check type
    template_map = {
        CheckType.HTTP: "nyxboard/healthcheck_form_http.html",
        CheckType.JSON_HTTP: "nyxboard/healthcheck_form_http.html",
        CheckType.DNS: "nyxboard/healthcheck_form_dns.html",
        CheckType.TCP: "nyxboard/healthcheck_form_tcp.html",
        CheckType.PING: "nyxboard/healthcheck_form_ping.html",
        CheckType.SMTP: "nyxboard/healthcheck_form_smtp.html",
        CheckType.IMAP: "nyxboard/healthcheck_form_imap.html",
        CheckType.JSON_METRICS: "nyxboard/healthcheck_form_json_metrics.html",
    }
    template_name = template_map.get(check_type, "nyxboard/healthcheck_form.html")

    return render(
        request,
        template_name,
        {
            "form": form,
            "service": service,
            "action": "Create",
            "check_type": check_type,
            "form_title": "Health Check",
            "back_url": reverse("nyxboard:service_detail", args=[service.id])
            if service
            else reverse("nyxboard:healthcheck_list"),
            "warnings": getattr(form, "warnings", []),
        },
    )


@nyxboard_login_required
def healthcheck_update(request, check_id):
    """
    Update an existing health check.
    """
    deleted = False
    health_check = HealthCheck.objects.filter(id=check_id).first()
    if health_check is None:
        if request.method != "POST" or "name" not in request.POST:
            get_object_or_404(HealthCheck, id=check_id)
        deleted = True
        health_check = HealthCheck(
            id=check_id, check_type=request.POST.get("check_type", CheckType.HTTP)
        )

    # Get the appropriate form class based on the check type
    # Use generic form as fallback to preserve check_type and data for unmapped types (legacy types)
    FormClass = FORM_CLASSES.get(health_check.check_type, GenericHealthCheckForm)

    if request.method == "POST":
        # Check if this is a quick-toggle of the disabled flag from the card
        if (
            "disabled" in request.POST and len(request.POST) == 2
        ):  # Just disabled and csrf token
            new_disabled_value = request.POST.get("disabled") == "1"
            HealthCheck.objects.filter(pk=check_id).update(disabled=new_disabled_value)

            # Redirect back to the referring page when it is on this host;
            # an external or missing referrer falls back to the dashboard.
            referer = request.META.get("HTTP_REFERER")
            if referer and url_has_allowed_host_and_scheme(
                referer,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(referer)
            return redirect("nyxboard:dashboard")

        # Normal form submission
        form = FormClass(request.POST, instance=health_check, require_revision=True)
        valid = form.is_valid()
        if deleted:
            form.add_error(None, EDIT_CONFLICT)
        elif valid:
            try:
                form.save()
            except DefinitionConflict:
                form.add_error(None, EDIT_CONFLICT)
            else:
                return redirect("nyxboard:healthcheck_detail", check_id=health_check.id)
    else:
        form = FormClass(instance=health_check, require_revision=True)

    # Select template based on check type
    template_map = {
        CheckType.HTTP: "nyxboard/healthcheck_form_http.html",
        CheckType.JSON_HTTP: "nyxboard/healthcheck_form_http.html",
        CheckType.DNS: "nyxboard/healthcheck_form_dns.html",
        CheckType.TCP: "nyxboard/healthcheck_form_tcp.html",
        CheckType.PING: "nyxboard/healthcheck_form_ping.html",
        CheckType.SMTP: "nyxboard/healthcheck_form_smtp.html",
        CheckType.IMAP: "nyxboard/healthcheck_form_imap.html",
        CheckType.JSON_METRICS: "nyxboard/healthcheck_form_json_metrics.html",
    }
    template_name = template_map.get(
        health_check.check_type, "nyxboard/healthcheck_form.html"
    )

    return render(
        request,
        template_name,
        {
            "form": form,
            "health_check": health_check,
            "action": "Update",
            "check_type": health_check.check_type,
            "form_title": "Health Check",
            "back_url": reverse("nyxboard:healthcheck_detail", args=[health_check.id]),
            "warnings": getattr(form, "warnings", []),
        },
    )


@nyxboard_login_required
def healthcheck_delete(request, check_id):
    """
    Delete a health check.
    """
    conflict = False
    if request.method == "POST":
        health_check, deleted = delete_confirmed(
            HealthCheck, check_id, request.POST.get("definition_revision")
        )
        if deleted:
            return redirect(
                "nyxboard:service_detail", service_id=health_check.service_id
            )
        conflict = True
    else:
        health_check = get_object_or_404(HealthCheck, id=check_id)
    return render(
        request,
        "nyxboard/healthcheck_confirm_delete.html",
        {
            "health_check": health_check,
            "definition_revision": deletion_revision(health_check),
            "conflict": DELETE_CONFLICT if conflict else None,
        },
        status=409 if conflict else 200,
    )


# HTMX-enabled views for health check updates
@nyxboard_login_required
def healthcheck_update_status(request, check_id):
    """
    Update the status of a health check for HTMX updates.
    This view is called periodically to check if a health check's status has changed.
    """
    health_check = get_object_or_404(HealthCheck, id=check_id)
    recent_results = health_check.results.order_by("-created_at", "-id")[:5]

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


@nyxboard_login_required
def healthcheck_trigger(request, check_id):
    """
    Manually trigger a health check to be run now.
    This marks the check as due immediately.
    """
    if request.method != "POST":
        return redirect("nyxboard:dashboard")

    health_check = get_object_or_404(HealthCheck, id=check_id)

    # Scheduling must not overwrite an agent's active lease or configuration.
    HealthCheck.objects.filter(pk=health_check.pk).update(next_check_time=int(time()))
    health_check.refresh_from_db()

    recent_results = list(health_check.results.order_by("-created_at", "-id")[:5])
    last_result = recent_results[0] if recent_results else None
    health_check.recent_results = recent_results

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


@nyxboard_login_required
def healthcheck_toggle_disabled(request, check_id):
    """
    Toggle the disabled status of a health check.
    Uses HTMX to update the check card.
    """
    if request.method != "POST":
        return redirect("nyxboard:dashboard")

    health_check = get_object_or_404(HealthCheck, id=check_id)

    # Each request inverts the database value once, even if reads interleave.
    # CASE uses the old disabled value; only disabling resets the schedule.
    HealthCheck.objects.filter(pk=health_check.pk).update(
        disabled=~F("disabled"),
        next_check_time=Case(
            When(disabled=False, then=int(time())),
            default=F("next_check_time"),
            output_field=PositiveIntegerField(),
        ),
    )
    health_check.refresh_from_db()

    recent_results = list(health_check.results.order_by("-created_at", "-id")[:5])
    last_result = recent_results[0] if recent_results else None
    health_check.recent_results = recent_results

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


THEME_BODY_LIMIT = 4096


def _unique_json_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate JSON member")
        data[key] = value
    return data


@nyxboard_login_required(json=True)
@require_POST
def set_theme(request):
    """
    Set the theme preference in the session.
    """
    try:
        body = request.read(THEME_BODY_LIMIT + 1)
        if len(body) > THEME_BODY_LIMIT:
            raise ValueError("theme body too large")
        data = json.loads(body, object_pairs_hook=_unique_json_object)
        if not isinstance(data, dict) or data.get("theme") not in ("light", "dark"):
            return JsonResponse(
                {"status": "error", "message": "Theme must be light or dark"},
                status=400,
            )
        theme = data["theme"]
        if theme == "light":
            if "theme" in request.session:
                del request.session["theme"]
        else:
            request.session["theme"] = theme
        return JsonResponse({"status": "success", "theme": theme})
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
