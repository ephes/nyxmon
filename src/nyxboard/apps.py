from django.apps import AppConfig


class NyxboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "nyxboard"

    def ready(self):
        from . import login_throttle  # noqa: F401
