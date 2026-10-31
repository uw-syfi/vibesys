"""A lazy module is declared by an import that pyright and tach read, beside the handle that runs.

``server.lazy_modules.lazy_module`` hides an import from the interpreter until first use. It would
hide it from the type checker and the dependency graph too, but for the import the declaration
puts in the ``if TYPE_CHECKING:`` branch. This test holds the two branches to each other: a
``lazy_module("a.b")`` that has no such import beside it, or names another module than the import
does, is a dependency the graph does not know about.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TypeGuard

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source, python_files, walk_nodes

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
FORM = (
    "if TYPE_CHECKING:\n    from <package> import <module>\nelse:\n    "
    '<module> = lazy_module("<package>.<module>")'
)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _is_lazy_call(node: ast.AST) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "lazy_module"
    )


def _imported(body: list[ast.stmt]) -> dict[str, str]:
    """Each name the branch's imports bind, with the module it names."""
    modules: dict[str, str] = {}
    for statement in body:
        if isinstance(statement, ast.ImportFrom) and statement.module and not statement.level:
            for alias in statement.names:
                modules[alias.asname or alias.name] = f"{statement.module}.{alias.name}"
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.asname:
                    modules[alias.asname] = alias.name
    return modules


def lazy_declaration_problems(source: str, filename: str) -> list[str]:
    """What is wrong with the ``lazy_module`` calls in ``source``, each as ``file:line: reason``."""
    tree = parse_source(source, filename)
    declared: set[int] = set()
    problems: list[str] = []
    for statement in tree.body:
        if not (isinstance(statement, ast.If) and _is_type_checking(statement.test)):
            continue
        imported = _imported(statement.body)
        for assignment in statement.orelse:
            if not (
                isinstance(assignment, ast.Assign)
                and _is_lazy_call(assignment.value)
                and len(assignment.targets) == 1
                and isinstance(assignment.targets[0], ast.Name)
            ):
                continue
            call = assignment.value
            declared.add(id(call))
            name = assignment.targets[0].id
            module = (
                call.args[0].value
                if len(call.args) == 1
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
                else None
            )
            where = f"{filename}:{assignment.lineno}"
            if module is None:
                problems.append(f"{where}: lazy_module takes one string literal; use {FORM}")
            elif imported.get(name) != module:
                problems.append(
                    f"{where}: {name} = lazy_module({module!r}) has no matching import in the "
                    f"TYPE_CHECKING branch (it imports {imported.get(name)!r} as {name}); "
                    f"the type checker and tach would not see {module}. Use {FORM}"
                )
    problems.extend(
        f"{filename}:{node.lineno}: lazy_module outside an `else:` of `if TYPE_CHECKING:` "
        f"with an import beside it; use {FORM}"
        for node in walk_nodes(tree)
        if _is_lazy_call(node) and id(node) not in declared
    )
    return problems


_IDENTIFIER = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True).filter(
    lambda name: name not in {"if", "in", "is", "as", "or", "and", "not", "for", "def"}
)


def _declaration(package: str, module: str, alias: str, target: str | None = None) -> str:
    """A compliant declaration of ``package.module`` as ``alias``; ``target`` is what runs."""
    return (
        "from typing import TYPE_CHECKING\n"
        "from server.lazy_modules import lazy_module\n\n"
        "if TYPE_CHECKING:\n"
        f"    from {package} import {module} as {alias}\n"
        "else:\n"
        f'    {alias} = lazy_module("{target or f"{package}.{module}"}")\n'
    )


@given(package=_IDENTIFIER, module=_IDENTIFIER, alias=_IDENTIFIER)
def test_a_declaration_with_its_import_beside_it_is_accepted(
    package: str, module: str, alias: str
) -> None:
    assert lazy_declaration_problems(_declaration(package, module, alias), "m.py") == []


@given(package=_IDENTIFIER, module=_IDENTIFIER, other=_IDENTIFIER, alias=_IDENTIFIER)
def test_a_handle_naming_another_module_than_its_import_is_rejected(
    package: str, module: str, other: str, alias: str
) -> None:
    if other == module:
        return
    source = _declaration(package, module, alias, target=f"{package}.{other}")
    [problem] = lazy_declaration_problems(source, "m.py")
    assert problem.startswith("m.py:7:")
    assert f"{package}.{other}" in problem
    assert "TYPE_CHECKING" in problem


def test_a_handle_with_no_import_beside_it_is_rejected_naming_the_line() -> None:
    source = "from server.lazy_modules import lazy_module\n\nexamples = lazy_module('server.x')\n"
    [problem] = lazy_declaration_problems(source, "m.py")
    assert problem.startswith("m.py:3:")
    assert "outside an `else:`" in problem


def test_a_handle_on_a_computed_name_is_rejected() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "from server.lazy_modules import lazy_module\n"
        "if TYPE_CHECKING:\n    from server import x\nelse:\n    x = lazy_module(NAME)\n"
    )
    [problem] = lazy_declaration_problems(source, "m.py")
    assert problem.startswith("m.py:6:")
    assert "string literal" in problem


def test_every_lazy_module_in_product_code_is_declared_by_an_import() -> None:
    problems = [
        problem
        for path in python_files(SOURCE_ROOT)
        if "lazy_module" in (source := path.read_text(encoding="utf-8"))
        for problem in lazy_declaration_problems(source, str(path.relative_to(SOURCE_ROOT.parent)))
    ]
    assert problems == [], "\n".join(problems)
