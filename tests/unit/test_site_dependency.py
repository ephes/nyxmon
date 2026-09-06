"""Parsing of ``data.site_dependency`` (plan section 7.1).

Malformed declarations must never raise and never change behaviour: they warn
once per check and leave the check unclassified, which is byte-for-byte
today's alerting.
"""

from __future__ import annotations

from typing import Any

import pytest

from nyxmon.service_layer.site_dependency import (
    INTERNET_REQUIREMENTS,
    PATH_NAMES,
    SiteDependency,
    reset_dependency_warning_state,
    resolve_site_dependency,
)


class FakeCheck:
    def __init__(self, check_id: int, data: Any) -> None:
        self.check_id = check_id
        self.data = data


@pytest.fixture(autouse=True)
def _reset_warnings():
    reset_dependency_warning_state()
    yield
    reset_dependency_warning_state()


def test_path_names_are_the_three_observed_dimensions() -> None:
    assert PATH_NAMES == ("dns", "ipv4", "ipv6")


def test_absent_declaration_is_unclassified() -> None:
    assert resolve_site_dependency(FakeCheck(1, {})) is None
    assert resolve_site_dependency(FakeCheck(1, None)) is None
    assert resolve_site_dependency(object()) is None


def test_none_shorthand_is_unclassified() -> None:
    assert resolve_site_dependency(FakeCheck(1, {"site_dependency": "none"})) is None
    assert resolve_site_dependency(FakeCheck(1, {"site_dependency": "NONE"})) is None


def test_internet_shorthand() -> None:
    dependency = resolve_site_dependency(FakeCheck(1, {"site_dependency": "internet"}))
    assert dependency == SiteDependency(INTERNET_REQUIREMENTS)
    assert dependency is not None
    assert dependency.requirements == (("dns",), ("ipv4", "ipv6"))
    assert dependency.path_names == ("dns", "ipv4", "ipv6")


def test_explicit_requirements() -> None:
    dependency = resolve_site_dependency(
        FakeCheck(1, {"site_dependency": {"requires": ["dns", ["ipv4", "ipv6"]]}})
    )
    assert dependency == SiteDependency((("dns",), ("ipv4", "ipv6")))

    single = resolve_site_dependency(
        FakeCheck(1, {"site_dependency": {"requires": ["ipv6"]}})
    )
    assert single == SiteDependency((("ipv6",),))


def test_requirements_are_normalised_and_deduplicated() -> None:
    dependency = resolve_site_dependency(
        FakeCheck(
            1,
            {
                "site_dependency": {
                    "requires": [" DNS ", ["IPv4", "ipv4", "ipv6"], "dns"]
                }
            },
        )
    )
    assert dependency == SiteDependency((("dns",), ("ipv4", "ipv6")))


def test_a_plain_data_dict_is_accepted() -> None:
    assert resolve_site_dependency({"site_dependency": "internet"}) == SiteDependency(
        INTERNET_REQUIREMENTS
    )


@pytest.mark.parametrize(
    "value",
    [
        "everything",
        123,
        [],
        {"requires": "internet"},
        {"requires": []},
        {"requires": ["ipv5"]},
        {"requires": [["ipv4", "carrier-pigeon"]]},
        {"requires": [[]]},
        {"requires": [None]},
        {"needs": ["dns"]},
    ],
)
def test_malformed_values_warn_once_and_stay_unclassified(value: Any, caplog) -> None:
    check = FakeCheck(42, {"site_dependency": value})
    with caplog.at_level("WARNING"):
        assert resolve_site_dependency(check) is None
        assert resolve_site_dependency(check) is None
    warnings = [
        record for record in caplog.records if "check_id=42" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "unclassified" in warnings[0].getMessage()


def test_each_check_warns_separately(caplog) -> None:
    with caplog.at_level("WARNING"):
        resolve_site_dependency(FakeCheck(1, {"site_dependency": "bogus"}))
        resolve_site_dependency(FakeCheck(2, {"site_dependency": "bogus"}))
    assert len(caplog.records) == 2
