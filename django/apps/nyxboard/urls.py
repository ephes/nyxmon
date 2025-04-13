from django.urls import path
from . import views

app_name = "nyxboard"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]
