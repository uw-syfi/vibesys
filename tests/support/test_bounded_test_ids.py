"""A test id over the limit stops collection with an error naming the id and the fix."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.bounded_test_ids import (
    MAX_NODEID_LENGTH,
    explain_overlong_ids,
    overlong_ids,
    pytest_xdist_node_collection_finished,
)
from tests.support.child_pytest import BARE_PYTEST, child_pythonpath, root_conftest_plugins

pytest_plugins = ["pytester"]

_REPOSITORY = Path(__file__).parents[2]


@given(
    short=st.lists(st.text(max_size=MAX_NODEID_LENGTH), max_size=5),
    long=st.lists(
        st.text(min_size=MAX_NODEID_LENGTH + 1, max_size=MAX_NODEID_LENGTH + 50), max_size=5
    ),
)
def test_exactly_the_ids_over_the_limit_are_reported(short: list[str], long: list[str]) -> None:
    reported = overlong_ids([*short, *long])

    assert len(reported) == len(long)
    assert all("characters)" in line and len(line) < 200 for line in reported)


@given(count=st.integers(min_value=1, max_value=40))
def test_the_error_is_short_whatever_the_number_of_ids(count: int) -> None:
    message = explain_overlong_ids(overlong_ids(["a" * 100_000] * count))

    assert f"{count} test ids" in message
    assert "pytest.param(value, id=" in message
    assert len(message) < 2_500


_PARAMETRIZED = """
import pytest

@pytest.mark.parametrize("value", [{value}])
def test_it(value):
    assert value
"""


def _collect(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> pytest.RunResult:
    monkeypatch.setenv("PYTHONPATH", child_pythonpath(_REPOSITORY))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    pytester.makepyfile(test_ids=_PARAMETRIZED.format(value=value))
    return pytester.runpytest(*BARE_PYTEST, "-p", "tests.support.bounded_test_ids")


def test_a_long_parameter_value_stops_the_session_naming_the_fix(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _collect(pytester, monkeypatch, '"é" * 40000')

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stdout.fnmatch_lines(["*1 test ids are over 500 characters*", "*test_ids.py::test_it*"])
    assert len(result.stdout.str()) < 2_500


def test_a_long_parameter_value_with_a_short_id_runs(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _collect(pytester, monkeypatch, 'pytest.param("é" * 40000, id="long")')

    result.assert_outcomes(passed=1)


def test_the_xdist_controller_checks_the_ids_a_worker_collected() -> None:
    pytest_xdist_node_collection_finished(["tests/test_a.py::test_a[short]"])
    with pytest.raises(pytest.exit.Exception, match="1 test ids are over"):
        pytest_xdist_node_collection_finished(["tests/test_a.py::test_a[" + "x" * 100_000 + "]"])


def test_the_root_conftest_loads_the_plugin() -> None:
    assert "tests.support.bounded_test_ids" in root_conftest_plugins(_REPOSITORY)
