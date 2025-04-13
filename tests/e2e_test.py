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
    check = FakeCheck(check_id="check1", data={})
    check = uow.store.checks.add(check)

    # When we run the checks
    bus.handle(ExecuteChecks())

    # Then the check result is generated
    assert len(uow.store.results.list()) == 1
    assert uow.store.results.list()[0].status == "ok"
