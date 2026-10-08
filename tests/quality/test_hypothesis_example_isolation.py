"""Quality contract: a Hypothesis example may not mutate state a strategy reads.

Hypothesis replays and shrinks a failing example by re-running the test body
against the same recorded choices. A body that grows a list or dict defined
outside it (typically one a ``sampled_from`` was built over) changes what the
next example may draw, so the replay diverges and Hypothesis raises
``FlakyStrategyDefinition`` at large budgets, where the example database is
used. State an example builds must live inside that example.

The scan is syntactic. In each function decorated with ``given`` it flags a
mutating method call or a subscript assignment whose root is a name the function
neither binds nor receives as a parameter. Imported modules are exempt: an
example may set an environment variable it then relies on. It cannot see
mutation hidden behind a method of a shared object; a shared fixture object
still has to be rebuilt per example.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

_ROOTS = ("src", "libs", "sdk", "tests", "scripts", "support")
_SKIPPED_DIRS = frozenset({".venv", "node_modules", "build", "dist", "__pycache__"})
_MUTATORS = frozenset(
    {
        "append",
        "extend",
        "add",
        "update",
        "setdefault",
        "pop",
        "popitem",
        "clear",
        "insert",
        "remove",
        "discard",
    }
)


def _is_given(decorator: ast.expr) -> bool:
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    return (isinstance(node, ast.Name) and node.id == "given") or (
        isinstance(node, ast.Attribute) and node.attr == "given"
    )


def _bound_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names created per call: parameters (also of nested defs) and assignments."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = node.args
            names |= {arg.arg for arg in args.posonlyargs + args.args + args.kwonlyargs}
            names |= {arg.arg for arg in (args.vararg, args.kwarg) if arg is not None}
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
    return names


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _mutated_targets(node: ast.Call | ast.stmt) -> list[ast.expr]:
    """The expressions one statement or call mutates in place."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MUTATORS
    ):
        return [node.func.value]
    if isinstance(node, ast.Assign):
        assigned = node.targets
    elif isinstance(node, ast.AugAssign):
        assigned = [node.target]
    elif isinstance(node, ast.Delete):
        assigned = node.targets
    else:
        return []
    return [target.value for target in assigned if isinstance(target, ast.Subscript)]


def shared_state_mutations(source: str) -> list[tuple[int, str]]:
    """Return ``(line, target)`` for each outer-state mutation inside a ``given`` body."""
    tree = ast.parse(source)
    imported = _imported_names(tree)
    sites: set[tuple[int, str]] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not any(_is_given(decorator) for decorator in function.decorator_list):
            continue
        bound = _bound_names(function)
        for node in ast.walk(function):
            if not isinstance(node, ast.Call | ast.stmt):
                continue
            for target in _mutated_targets(node):
                root = _root_name(target)
                if root is not None and root not in bound and root not in imported:
                    sites.add((node.lineno, ast.unparse(target)))
    return sorted(sites)


def test_a_pool_grown_inside_an_example_is_flagged() -> None:
    source = """
pool = ["a"]

@given(st.data())
def check(data):
    data.draw(st.sampled_from(pool))
    pool.extend(["b"])
"""
    assert shared_state_mutations(source) == [(7, "pool")]


def test_a_mapping_written_through_a_subscript_is_flagged() -> None:
    source = """
seen = {}

def outer():
    @given(st.integers())
    def check(value):
        seen[value] = True
"""
    assert shared_state_mutations(source) == [(7, "seen")]


def test_state_built_inside_the_example_is_not_flagged() -> None:
    source = """
@given(st.data())
def check(data):
    pool = ["a"]
    pool.extend(data.draw(st.lists(st.text())))
    index = {}
    index[1] = pool
"""
    assert shared_state_mutations(source) == []


def test_a_mutator_outside_a_given_body_is_not_flagged() -> None:
    source = """
log = []

def helper():
    log.append(1)
"""
    assert shared_state_mutations(source) == []


def test_an_imported_module_attribute_is_not_flagged() -> None:
    source = """
import os

@given(st.integers())
def check(value):
    os.environ.setdefault("X", str(value))
"""
    assert shared_state_mutations(source) == []


def _python_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for root in _ROOTS:
        for directory, subdirectories, names in os.walk(repo_root / root):
            subdirectories[:] = [name for name in subdirectories if name not in _SKIPPED_DIRS]
            files.extend(Path(directory) / name for name in names if name.endswith(".py"))
    return sorted(files)


def test_no_given_body_mutates_state_defined_outside_it() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    offenders = [
        f"{path.relative_to(repo_root)}:{line} mutates {target}"
        for path in _python_files(repo_root)
        for source in [path.read_text(encoding="utf-8")]
        if "given" in source
        for line, target in shared_state_mutations(source)
    ]
    if offenders:
        pytest.fail(
            "A Hypothesis example mutates state defined outside it, so a failing "
            "example cannot be replayed or shrunk. Build the state inside the example:\n"
            + "\n".join(offenders)
        )
