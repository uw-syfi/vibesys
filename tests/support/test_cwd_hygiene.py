"""A working-directory change made by one test never reaches a later test."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_LEAKY_TESTS = """
import os
from pathlib import Path

def test_a_leaks():
    os.chdir({directory!r})

def test_b_probe():
    assert Path.cwd() != Path({directory!r}).resolve()
"""


@pytest.mark.parametrize("hygiene", ["with", "without"])
def test_a_directory_change_does_not_outlive_its_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, hygiene: str
) -> None:
    repository = Path(__file__).parents[2]
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(repository), os.environ.get("PYTHONPATH", "")))
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    directory = str(pytester.mkdir("elsewhere"))
    pytester.makepyfile(test_leak=_LEAKY_TESTS.format(directory=directory))
    args = ["-p", "tests.support.cwd_hygiene"] if hygiene == "with" else []

    outcome = int(pytester.runpytest_subprocess("-p", "no:cacheprovider", *args).ret)

    assert (outcome == 0) is (hygiene == "with")
