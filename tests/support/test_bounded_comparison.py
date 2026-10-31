"""A failed ``==`` over a large operand is explained in a few lines naming the first difference."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.bounded_comparison import (
    CONTEXT,
    LARGE_OPERAND,
    explain_comparison,
)
from tests.support.child_pytest import BARE_PYTEST, child_pythonpath, root_conftest_plugins

pytest_plugins = ["pytester"]

_REPOSITORY = Path(__file__).parents[2]
_SIZES = st.integers(min_value=LARGE_OPERAND + 1, max_value=400_000)


def _flipped(value: bytes, position: int) -> bytes:
    return value[:position] + bytes([value[position] ^ 1]) + value[position + 1 :]


@given(size=_SIZES, data=st.data())
def test_a_large_bytes_comparison_names_the_first_differing_offset(
    size: int, data: st.DataObject
) -> None:
    position = data.draw(st.integers(min_value=0, max_value=size - 1))
    left = bytes(size)
    right = _flipped(left, position)

    lines = explain_comparison("==", left, right)

    assert lines is not None
    assert f"  first difference at offset {position}" in lines
    assert len(lines) <= 6
    assert all(len(line) <= 8 * CONTEXT + 40 for line in lines)


@given(size=_SIZES, data=st.data())
def test_a_large_text_comparison_names_the_first_differing_offset(
    size: int, data: st.DataObject
) -> None:
    position = data.draw(st.integers(min_value=0, max_value=size - 1))
    left = "é" * size
    right = left[:position] + "e" + left[position + 1 :]

    lines = explain_comparison("==", left, right)

    assert lines is not None
    assert f"  first difference at offset {position}" in lines


@given(size=st.integers(min_value=LARGE_OPERAND, max_value=30_000), data=st.data())
def test_a_large_collection_is_explained_from_its_printed_form(
    size: int, data: st.DataObject
) -> None:
    position = data.draw(st.integers(min_value=0, max_value=size - 1))
    left = list(range(size))
    right = [*left[:position], -1, *left[position + 1 :]]

    lines = explain_comparison("==", left, right)

    assert lines is not None
    assert len(lines) <= 6
    assert sum(map(len, lines)) < 1_000


@given(left=st.binary(max_size=LARGE_OPERAND), right=st.binary(max_size=LARGE_OPERAND))
def test_small_operands_keep_the_pytest_diff(left: bytes, right: bytes) -> None:
    assert explain_comparison("==", left, right) is None


@pytest.mark.parametrize("op", ["!=", "in", "not in", "is", "<"])
def test_only_equality_is_explained(op: str) -> None:
    assert explain_comparison(op, bytes(LARGE_OPERAND + 1), bytes(LARGE_OPERAND + 2)) is None


def test_operands_that_print_alike_say_so() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "x" * (LARGE_OPERAND + 1)

    lines = explain_comparison("==", Opaque(), Opaque())

    assert lines is not None
    assert any("identical" in line for line in lines)


def test_the_root_conftest_loads_the_plugin() -> None:
    assert "tests.support.bounded_comparison" in root_conftest_plugins(_REPOSITORY)


_LARGE_REPLAYS = f"""
def test_a_stale_replay():
    golden = bytes({LARGE_OPERAND} * 40)
    assert golden == golden[:1234] + b"x" + golden[1235:], "Regenerate: the command"
"""


@pytest.mark.parametrize("verbosity", ["-q", "-v", "-vv"])
def test_a_failed_comparison_prints_no_diff_at_any_verbosity(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, verbosity: str
) -> None:
    monkeypatch.setenv("PYTHONPATH", child_pythonpath(_REPOSITORY))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("CI", "true")
    pytester.makepyfile(test_stale=_LARGE_REPLAYS)

    result = pytester.runpytest(*BARE_PYTEST, "-p", "tests.support.bounded_comparison", verbosity)

    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "first difference at offset 1234" in output
    assert "Regenerate: the command" in output
    assert "Full diff" not in output
    assert len(output) < 5_000
