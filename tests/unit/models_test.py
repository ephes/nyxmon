import pytest

from nyxmon.domain import Check, Result, OK, ERROR, CheckResult


@pytest.mark.parametrize(
    "status, expected",
    [
        (OK, True),
        (
            ERROR,
            False,
        ),
    ],
)
def test_check_result_passed(status, expected):
    # Given a check and a result
    check = Check(check_id=1, service_id=1, check_type="http", url="http://.", data={})
    result = Result(check_id=1, status=status, data={})

    # When we build a CheckResult
    check_result = CheckResult(check=check, result=result)

    # Then the check result should match the expected status
    assert check_result.passed == expected, (
        f"Expected {expected}, but got {check_result.passed}"
    )
