import time
from nyxmon.domain import Result
from nyxmon.domain.commands import ExecuteChecks


def test_run_checks_empty(bus):
    # Given a message bus with no pending checks
    uow = bus.uow
    assert uow.store.checks.list() == []

    # When we run the checks
    bus.handle(ExecuteChecks())

    # Then no check results are generated
    assert uow.store.checks.list() == []


class FakeCheck:
    def __init__(self, *, check_id, data):
        self.check_id = check_id
        self.data = data
        self.result = None
        self.events = []

    def execute(self):
        # Simulate executing the check and generating a result
        self.result = Result(result_id="result1", status="ok", data=self.data)


def test_run_checks_with_result(bus):
    # Given a message bus with a pending check
    uow = bus.uow
    check = FakeCheck(check_id=1, data={})
    check = uow.store.checks.add(check)

    # When we run the checks
    bus.handle(ExecuteChecks())

    # Then the check result is generated
    assert len(uow.store.results.list()) == 1
    assert uow.store.results.list()[0].status == "ok"


class SlowCheck:
    def __init__(self, *, check_id, data, delay_seconds=0.5):
        self.check_id = check_id
        self.data = data
        self.result = None
        self.events = []
        self.delay_seconds = delay_seconds
        self.executed_at = None

    def execute(self):
        # Simulate a slow HTTP check
        time.sleep(self.delay_seconds)
        self.executed_at = time.time()
        self.result = Result(
            result_id=f"result-{self.check_id}", status="ok", data=self.data
        )


def test_run_checks_concurrently(bus):
    # Given an uow with empty checks and results
    uow = bus.uow
    uow.store.checks.checks = {}
    uow.store.results.results = {}

    # And multiple slow checks
    num_checks = 5
    delay_per_check = 0.2  # seconds

    for i in range(num_checks):
        check = SlowCheck(check_id=i, data={}, delay_seconds=delay_per_check)
        uow.store.checks.add(check)

    # When we run the checks
    start_time = time.time()
    bus.handle(ExecuteChecks())
    total_time = time.time() - start_time

    # Then:
    # 1. All checks have results
    assert len(uow.store.results.list()) == num_checks

    # 2. The total execution time should be significantly less than
    # running them sequentially (which would be num_checks * delay_per_check)
    # Allow some overhead for test execution
    sequential_time = num_checks * delay_per_check
    assert total_time < sequential_time, (
        f"Expected concurrent execution to be faster than {sequential_time:.2f}s, "
        f"but it took {total_time:.2f}s"
    )

    # 3. All checks should have executed successfully
    for i, check in enumerate(uow.store.checks.list()):
        assert check.executed_at is not None
        assert check.result is not None
        assert check.result.status == "ok"
