"""A session that leaves new entries in the repository root fails and names them."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.child_pytest import BARE_PYTEST
from tests.support.tree_hygiene import top_level_entries

pytest_plugins = ["pytester"]

_NAMES = st.text(st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=8)


@given(created=st.sets(_NAMES, max_size=4), hidden=st.sets(_NAMES, max_size=3))
def test_only_non_hidden_entries_count_toward_a_leak(
    created: set[str], hidden: set[str], tmp_path_factory: pytest.TempPathFactory
) -> None:
    root = tmp_path_factory.mktemp("root")
    for name in created:
        (root / name).touch()
    for name in hidden:
        (root / f".{name}").touch()

    assert top_level_entries(root) == frozenset(created) - {"build", "dist", "node_modules"}


_FINISH_TOOLING = """
def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):
        return
    open("durations-1.json", "w").write("{}")
"""


@pytest.mark.parametrize(
    ("leaks", "workers"),
    [(True, []), (False, []), (True, ["-n", "1"])],
    ids=["leak", "clean", "xdist"],
)
def test_a_session_that_writes_into_its_root_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, leaks: bool, workers: list[str]
) -> None:
    repository = Path(__file__).parents[2]
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(repository), os.environ.get("PYTHONPATH", "")))
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    for variable in tuple(os.environ):
        if variable.startswith(("COV_CORE_", "COVERAGE_")):
            monkeypatch.delenv(variable)
    body = 'Path("candidate-1").mkdir()' if leaks else "pass"
    pytester.makepyfile(test_leak=f"from pathlib import Path\n\ndef test_it():\n    {body}\n")
    # Output that tooling writes at session finish is not a test's leak.
    pytester.makeconftest(_FINISH_TOOLING)

    # Workers need real processes; a single process runs in this one.
    run = pytester.runpytest_subprocess if workers else pytester.runpytest
    result = run(*BARE_PYTEST, "-p", "tests.support.tree_hygiene", *workers)

    assert (int(result.ret) != 0) is leaks
    if leaks:
        result.stdout.fnmatch_lines(
            ["*tests left new entries in the repository root: *candidate-1*"]
        )
        assert result.stdout.str().count("tests left new entries") == 1
