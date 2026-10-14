"""The seed-exploration step picks exactly the tests a diff touches and reports what the cap cut."""

from __future__ import annotations

import dataclasses
import difflib
import json
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from scripts.explore_changed_tests import (
    LISTED_UNEXPLORED,
    PYTEST_NO_TESTS_COLLECTED,
    Explored,
    changed_lines,
    function_spans,
    outcome,
    read_report,
    selected_ids,
    suite_pathspecs,
    summary,
)

if TYPE_CHECKING:
    import pytest

pytest_plugins = ["pytester"]

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "libs" / "vs-sim" / "pytest_plugin"

MODULE = textwrap.dedent(
    """\
    import asyncio

    HELPER = 1


    async def test_one():
        assert True


    @decorated
    def test_two():
        assert True


    class TestGroup:
        def test_three(self):
            assert True

        def helper(self):
            return 1
    """
)


def _diff_after_editing(name: str, edited: str) -> str:
    """What `git diff -U0` prints for editing `name` from MODULE to `edited`."""
    body = difflib.unified_diff(
        MODULE.splitlines(keepends=True),
        edited.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
        n=0,
    )
    return f"diff --git a/{name} b/{name}\n" + "".join(body)


def _ids(edited: str) -> list[str]:
    name = "test_mod.py"
    diff = _diff_after_editing(name, edited)
    return selected_ids(diff, lambda _path: edited)


def test_a_change_inside_a_test_selects_only_that_test() -> None:
    edited = MODULE.replace(
        "def test_three(self):\n        assert True", "def test_three(self):\n        assert 1"
    )
    assert _ids(edited) == ["test_mod.py::TestGroup::test_three"]


def test_a_changed_decorator_selects_the_decorated_test() -> None:
    assert _ids(MODULE.replace("@decorated", "@decorated_differently")) == ["test_mod.py::test_two"]


def test_a_change_outside_any_test_selects_the_whole_file() -> None:
    assert _ids(MODULE.replace("HELPER = 1", "HELPER = 2")) == ["test_mod.py"]


def test_no_change_selects_nothing() -> None:
    assert _ids(MODULE) == []


def test_only_test_modules_are_selected() -> None:
    diff = "diff --git a/src/mod.py b/src/mod.py\n+++ b/src/mod.py\n@@ -1 +1 @@\n"
    assert selected_ids(diff, lambda _path: "") == []


@given(st.integers(1, 40), st.integers(0, 5), st.integers(0, 5))
def test_every_hunk_range_covers_its_new_lines(start: int, count: int, old: int) -> None:
    diff = f"diff --git a/t.py b/t.py\n+++ b/t.py\n@@ -{old},{old} +{start},{count} @@\n"
    ((first, last),) = changed_lines(diff)["t.py"]
    assert first == start
    assert last == start + max(count, 1) - 1 + (1 if count == 0 else 0)


def test_function_spans_include_class_methods_and_skip_helpers() -> None:
    assert [span.node for span in function_spans(MODULE)] == [
        "test_one",
        "test_two",
        "TestGroup::test_three",
    ]


def test_only_the_suites_testpaths_are_explored() -> None:
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()

    pathspecs = suite_pathspecs(pyproject)

    assert ":(glob)tests/**/*.py" in pathspecs
    assert all("examples" not in spec for spec in pathspecs)


_ROWS = st.lists(
    st.builds(
        Explored,
        test=st.from_regex(r"tests/test_[a-z]{1,6}\.py::test_[a-z]{1,6}", fullmatch=True),
        runs=st.integers(0, 24),
        planned=st.integers(0, 24),
        outcome=st.sampled_from(["passed", "failed", "skipped", "not explored"]),
    ),
    max_size=80,
    unique_by=lambda row: row.test,
)


@given(_ROWS)
def test_a_report_reads_back_the_rows_it_was_written_from(rows: list[Explored]) -> None:
    text = "".join(f"{json.dumps(dataclasses.asdict(row))}\n" for row in rows)
    assert read_report(text) == rows


@given(_ROWS, st.floats(1, 600))
def test_the_summary_names_every_failure_and_the_cut_tests_it_has_room_for(
    rows: list[Explored], cap: float
) -> None:
    text = summary(rows, selections=len(rows), cap=cap, elapsed=cap, verdict="passed")

    listed = [
        line.removeprefix("- `").removesuffix("`")
        for line in text.splitlines()
        if line.startswith("- `")
    ]
    failed = [row.test for row in rows if row.outcome == "failed"]
    unexplored = [row.test for row in rows if row.outcome == "not explored"]
    assert listed == failed + unexplored[:LISTED_UNEXPLORED]
    assert f"{len(rows) - len(unexplored)} explored" in text
    assert (f"and {len(unexplored) - LISTED_UNEXPLORED} more" in text) == (
        len(unexplored) > LISTED_UNEXPLORED
    )


@given(code=st.integers(-9, 5), capped=st.booleans())
def test_only_a_failing_test_fails_a_capped_run(code: int, *, capped: bool) -> None:
    failed, verdict = outcome(code, capped=capped)

    if capped:
        assert failed == (code == 1)
    else:
        assert failed == (code not in {0, PYTEST_NO_TESTS_COLLECTED})
    assert (verdict == "FAILED") == failed


def test_the_plugins_exploration_report_is_what_the_summary_reads(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(PLUGIN_DIR))
    pytester.makeini("[pytest]\naddopts = -p vs_sim_pytest -p no:cacheprovider -p no:tach\n")
    pytester.makepyfile(
        test_pair="async def test_sim():\n    pass\n\ndef test_plain():\n    pass\n"
    )

    pytester.runpytest_inprocess(
        "--sim-explore=4", "--sim-repeat=2", "--sim-explore-report=explored.jsonl"
    ).assert_outcomes(passed=2)

    rows = read_report((pytester.path / "explored.jsonl").read_text())
    assert sorted(rows, key=lambda row: row.test) == [
        Explored("test_pair.py::test_plain", 2, 2, "passed"),
        Explored("test_pair.py::test_sim", 4, 4, "passed"),
    ]
