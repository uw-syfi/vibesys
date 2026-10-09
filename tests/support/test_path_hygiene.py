"""A ``sys.path`` edit made by one test file or test never reaches a later test."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_LEAKY_COLLECTION = """
import sys
sys.path.insert(0, {directory!r})
"""

_PROBE = """
import sys

def test_the_leaked_directory_is_gone():
    assert {directory!r} not in sys.path
"""

_LEAKY_TEST = """
import sys

def test_a_leaks():
    sys.path.insert(0, {directory!r})

def test_b_probe():
    assert {directory!r} not in sys.path
"""


def _run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, hygiene: str) -> int:
    repository = Path(__file__).parents[2]
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(repository), os.environ.get("PYTHONPATH", "")))
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    args = ["-p", "tests.support.path_hygiene"] if hygiene == "with" else []
    return int(pytester.runpytest_subprocess("-p", "no:cacheprovider", *args).ret)


@pytest.mark.parametrize("hygiene", ["with", "without"])
@pytest.mark.parametrize("leak", ["collection", "test"])
def test_path_edits_do_not_outlive_their_source(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, leak: str, hygiene: str
) -> None:
    directory = str(pytester.mkdir("shadow"))
    if leak == "collection":
        pytester.makepyfile(
            test_a=_LEAKY_COLLECTION.format(directory=directory) + "\ndef test_a(): pass\n",
            test_b=_PROBE.format(directory=directory),
        )
    else:
        pytester.makepyfile(test_leak=_LEAKY_TEST.format(directory=directory))

    outcome = _run(pytester, monkeypatch, hygiene=hygiene)

    assert (outcome == 0) is (hygiene == "with")
