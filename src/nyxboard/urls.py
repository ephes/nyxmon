from django.urls import path
from . import views

app_name = "nyxboard"

urlpatterns = [
    # Dashboard
    path("", views.dashboard, name="dashboard"),
    # Service CRUD
    path("services/", views.service_list, name="service_list"),
    path("services/create/", views.service_create, name="service_create"),
    path("services/<int:service_id>/", views.service_detail, name="service_detail"),
    path(
        "services/<int:service_id>/update/", views.service_update, name="service_update"
    ),
    path(
        "services/<int:service_id>/delete/", views.service_delete, name="service_delete"
    ),
    # HealthCheck CRUD
    path("healthchecks/", views.healthcheck_list, name="healthcheck_list"),
    path("healthchecks/create/", views.healthcheck_create, name="healthcheck_create"),
    path(
        "services/<int:service_id>/healthchecks/create/",
        views.healthcheck_create,
        name="healthcheck_create_for_service",
    ),
    path(
        "healthchecks/<int:check_id>/",
        views.healthcheck_detail,
        name="healthcheck_detail",
    ),
    path(
        "healthchecks/<int:check_id>/update/",
        views.healthcheck_update,
        name="healthcheck_update",
    ),
    path(
        "healthchecks/<int:check_id>/delete/",
        views.healthcheck_delete,
        name="healthcheck_delete",
    ),
    # HTMX-enabled endpoints
    path(
        "healthchecks/<int:check_id>/status/",
        views.healthcheck_update_status,
        name="healthcheck_update_status",
    ),
    path(
        "healthchecks/<int:check_id>/trigger/",
        views.healthcheck_trigger,
        name="healthcheck_trigger",
    ),
    path(
        "healthchecks/<int:check_id>/toggle-disabled/",
        views.healthcheck_toggle_disabled,
        name="healthcheck_toggle_disabled",
    ),
    # Theme setting endpoint
    path("set-theme/", views.set_theme, name="set_theme"),
]
