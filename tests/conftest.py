import pytest


@pytest.fixture
def bus():
    """
    Fixture to create a MessageBus instance for testing.
    """
    from nyxmon.bootstrap import bootstrap

    return bootstrap()


@pytest.fixture
def anyio_backend():
    return "asyncio"
