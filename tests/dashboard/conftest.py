import pytest

from nyxmon.domain import ResultStatus

from nyxmon.nyxboard.models import Result, Service, HealthCheck


@pytest.fixture
def service_factory():
    """
    Factory fixture to create Service instances.
    """

    def _create_service(name="Test Service"):
        return Service.objects.create(name=name)

    return _create_service


@pytest.fixture
def healthcheck_factory():
    """
    Factory fixture to create HealthCheck instances.
    """

    def _create_healthcheck(service, **kwargs):
        defaults = {
            "check_type": "http",
            "url": "https://example.com",
            "check_interval": 300,
        }
        defaults.update(kwargs)
        return HealthCheck.objects.create(service=service, **defaults)

    return _create_healthcheck


@pytest.fixture
def result_factory():
    """
    Factory fixture to create Result instances.
    """

    def _create_result(health_check, status=ResultStatus.OK, data=None):
        if data is None:
            data = {"response_time": 0.5}
        return Result.objects.create(
            health_check=health_check, status=status, data=data
        )

    return _create_result
