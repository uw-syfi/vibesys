"""The seed-exploration selector picks exactly the tests a diff touches."""

from __future__ import annotations

import difflib
import textwrap

from hypothesis import given
from hypothesis import strategies as st
from scripts.explore_changed_tests import changed_lines, function_spans, selected_ids

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


def function_spans_include_class_methods_and_skip_helpers() -> None:
    assert [span.node for span in function_spans(MODULE)] == [
        "test_one",
        "test_two",
        "TestGroup::test_three",
    ]
