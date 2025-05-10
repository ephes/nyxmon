from nyxmon.domain import Result
from nyxmon.bootstrap import bootstrap
from nyxmon.domain.commands import ExecuteChecks


class DummyRunner:
    def run_all(self, checks, callback):
        print("runner run_all called! ", checks)
        for check in checks:
            # Simulate running the check and calling the callback with a result
            print("check", check)
            result = Result(
                result_id=f"result-{check.check_id}",
                check_id=check.check_id,
                status="ok",
                data={},
            )
            callback(result)


def test_run_checks_empty():
    # Given a message bus with no pending checks
    bus = bootstrap(runner=DummyRunner())
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

    def add_result(self, result):
        self.result = result


def test_run_checks_with_result():
    # Given a message bus with a pending check
    bus = bootstrap(runner=DummyRunner())

    uow = bus.uow
    check = FakeCheck(check_id=1, data={})
    uow.store.checks.add(check)
    assert len(uow.store.checks.list()) == 1

    # When we run the checks
    bus.handle(ExecuteChecks(checks=uow.store.checks.list()))

    # Then the check result is generated
    assert len(uow.store.results.list()) == 1
    assert uow.store.results.list()[0].status == "ok"
