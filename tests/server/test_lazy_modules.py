"""A lazy module imports nothing until an attribute is read, then behaves as the module."""

from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from server import lazy_module

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A module on the path that records each time it is executed, and is forgotten after."""
    name = "lazy_module_probe"
    record = tmp_path / "executions"
    (tmp_path / f"{name}.py").write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path

            with Path({str(record)!r}).open("a") as executions:
                executions.write("x")
            VALUE = 7
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    sys.modules.pop(name, None)


def _executions(tmp_path: Path) -> int:
    record = tmp_path / "executions"
    return len(record.read_text()) if record.exists() else 0


def test_the_module_is_not_imported_until_an_attribute_is_read(probe: str, tmp_path: Path) -> None:
    handle = lazy_module(probe)

    assert probe not in sys.modules
    assert _executions(tmp_path) == 0

    assert handle.VALUE == 7
    assert _executions(tmp_path) == 1


def test_the_module_runs_once_however_often_it_is_read(probe: str, tmp_path: Path) -> None:
    handle = lazy_module(probe)

    reads = [handle.VALUE for _ in range(5)]

    assert reads == [7] * 5
    assert _executions(tmp_path) == 1


@given(name=st.sampled_from(["__wrapped__", "__path__", "__file__x__", "__anything__"]))
def test_a_dunder_asked_of_the_handle_does_not_import_the_module(name: str) -> None:
    handle = lazy_module("lazy_module_never_imported")

    assert not hasattr(handle, name)
    assert "lazy_module_never_imported" not in sys.modules


def test_a_module_that_is_missing_fails_where_it_is_first_used() -> None:
    handle = lazy_module("lazy_module_that_does_not_exist")

    with pytest.raises(ModuleNotFoundError, match="lazy_module_that_does_not_exist"):
        assert handle.anything is None
