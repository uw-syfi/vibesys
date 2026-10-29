"""Quality contract: a property-based regression test pins the bug with ``@example``.

CI runs Hypothesis with the ``ci`` profile: derandomized, 10 examples. Ten random draws may
never reach the input that triggered a bug, so a ``@given`` regression test with no
``@example`` can pass on the buggy code (this happened to two fixes in one day). An
``@example`` always runs, under every profile, so it is what makes the test fail at the
merge base.

A ``@given`` test is a *regression test* when any of these holds:

* it carries ``@pytest.mark.regression`` (the marker to use for a bug fix; it is registered
  in ``pyproject.toml``);
* its name contains the word ``regression``;
* its docstring contains the word "regression" or an issue reference such as ``#3084``.

Every regression test must carry at least one ``@example(...)`` (or ``@hypothesis.example``)
that reproduces the reported input. The scan is syntactic and reads ``src``, ``libs``,
``sdk``, ``tests``, ``scripts`` and ``support``.

Existing violations are listed in ``regression_pin_baseline.jsonl`` as ``{"path", "test"}``
lines. The list only shrinks: a new violation fails, and a listed test that is now pinned
(or gone) must be deleted from the file.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, python_files, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("regression_pin_baseline.jsonl")
_ROOTS = ("src", "libs", "sdk", "tests", "scripts", "support")
_SKIPPED_DIRS = frozenset({".venv", "node_modules", "build", "dist", "__pycache__"})
_REGRESSION_WORD = re.compile(r"regression", re.IGNORECASE)
_ISSUE_REF = re.compile(r"#\d+")

FIX_HINT = (
    "pin the reported input with `@example(...)` above the test (it runs under every "
    "Hypothesis profile, so the test fails on the unfixed code even at CI's 10 examples); "
    "see the testing skill, rule 'Bug fixes'"
)


def _callee(decorator: ast.expr) -> ast.expr:
    return decorator.func if isinstance(decorator, ast.Call) else decorator


def _is_named(decorator: ast.expr, name: str) -> bool:
    node = _callee(decorator)
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


def _is_regression_marker(decorator: ast.expr) -> bool:
    node = _callee(decorator)
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "regression"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    )


def _marks_regression(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    doc = ast.get_docstring(function) or ""
    return (
        any(_is_regression_marker(d) for d in function.decorator_list)
        or _REGRESSION_WORD.search(function.name) is not None
        or _REGRESSION_WORD.search(doc) is not None
        or _ISSUE_REF.search(doc) is not None
    )


def unpinned_regressions(source: str) -> list[tuple[int, str]]:
    """``(line, test name)`` of each ``@given`` regression test without an ``@example``."""
    if "given" not in source:
        return []
    found: list[tuple[int, str]] = []
    for node in walk_nodes(parse_source(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        decorators = node.decorator_list
        if not any(_is_named(d, "given") for d in decorators):
            continue
        if _marks_regression(node) and not any(_is_named(d, "example") for d in decorators):
            found.append((node.lineno, node.name))
    return found


def _problem(path: str, line: int, name: str) -> str:
    return f"{path}:{line}: regression test `{name}` has @given but no @example; {FIX_HINT}"


def scan(paths: list[Path]) -> dict[tuple[str, str], int]:
    """Line of each unpinned regression test, by ``(repo-relative path, test name)``."""
    found: dict[tuple[str, str], int] = {}
    for path in paths:
        relative = path.relative_to(REPO).as_posix()
        for line, name in unpinned_regressions(path.read_text(encoding="utf-8")):
            found[(relative, name)] = line
    return found


def ratchet_problems(found: dict[tuple[str, str], int], allowed: set[tuple[str, str]]) -> list[str]:
    """New violations, and baseline entries that no longer violate."""
    problems = [
        _problem(p, line, n) for (p, n), line in sorted(found.items()) if (p, n) not in allowed
    ]
    problems += [
        f"{p}: `{n}` is pinned or gone; delete its line from {BASELINE.name} (the list only shrinks)"
        for p, n in sorted(allowed - found.keys())
    ]
    return problems


def load_baseline() -> set[tuple[str, str]]:
    lines = [json.loads(x) for x in BASELINE.read_text(encoding="utf-8").splitlines() if x]
    entries = {(e["path"], e["test"]) for e in lines}
    assert len(entries) == len(lines), "duplicate entry in the baseline"
    return entries


def _repo_files() -> list[Path]:
    return [
        p
        for root in _ROOTS
        for p in python_files(REPO / root)
        if not _SKIPPED_DIRS & set(p.relative_to(REPO).parts)
    ]


def test_every_property_regression_test_pins_its_input() -> None:
    problems = ratchet_problems(scan(_repo_files()), load_baseline())
    assert not problems, "\n".join(problems)


# ---- the checker itself ---------------------------------------------------------------

_PINNED = """
@pytest.mark.regression
@given(st.integers())
@example(0)
def test_a(x): ...
"""
_UNPINNED = {
    "marker": "@pytest.mark.regression\n@given(st.integers())\ndef test_a(x): ...\n",
    "name": "@given(st.integers())\ndef test_regression_of_zero(x): ...\n",
    "docstring word": '@given(st.integers())\ndef test_a(x):\n    """Regression for zero."""\n',
    "issue ref": '@given(st.integers())\ndef test_a(x):\n    """Fixes #3084."""\n',
    "async": "@given(st.integers())\nasync def test_regression_a(x): ...\n",
    "hypothesis.given": "@hypothesis.given(st.integers())\ndef test_regression_a(x): ...\n",
}


@pytest.mark.parametrize("source", _UNPINNED.values(), ids=_UNPINNED.keys())
def test_an_unpinned_regression_test_is_flagged(source: str) -> None:
    assert [name for _, name in unpinned_regressions(source)] in (
        ["test_a"],
        ["test_regression_of_zero"],
        ["test_regression_a"],
    )


def test_a_pinned_regression_test_passes() -> None:
    assert unpinned_regressions(_PINNED) == []
    assert unpinned_regressions(_PINNED.replace("@example(0)", "@hypothesis.example(0)")) == []


def test_a_plain_property_needs_no_example() -> None:
    assert unpinned_regressions("@given(st.integers())\ndef test_a(x): ...\n") == []


def test_a_regression_test_without_given_needs_no_example() -> None:
    assert unpinned_regressions("def test_regression_a(): ...\n") == []


def test_the_message_names_the_test_and_file_and_the_fix() -> None:
    [message] = ratchet_problems({("tests/x/test_y.py", "test_z"): 7}, set())
    assert message.startswith("tests/x/test_y.py:7:")
    assert "`test_z`" in message
    assert "@example(...)" in message


def test_the_baseline_only_shrinks() -> None:
    key = ("a.py", "test_a")
    assert ratchet_problems({key: 1}, set())
    assert ratchet_problems({}, {key})
    assert ratchet_problems({key: 1}, {key}) == []
