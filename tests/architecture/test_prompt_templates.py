"""Architecture contract: agent prompts are rendered from templates.

Python passes data; templates own the wording, conditionals, and loops. This
check walks every prompt sink in ``src/`` (the message of an agent ``.turn``
call and every ``system_prompt=`` argument) back through local variables,
module constants, and functions defined in or imported from first-party
modules, and reports text assembled in Python: string literals, ``+``,
``+=``, ``%``, f-strings, ``.format``, and ``.join``. It also reports those
operations applied to a value returned by a ``render*`` call, ``.format`` on a
module-level string constant (template text kept in Python), and any
``RenderedPrompt(...)`` construction outside the renderer.

Every module is enforced except those in ``_NOT_YET_MIGRATED``. That list only
shrinks: a listed module that no longer has a violation fails the check until
it is removed from the list.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_NOT_YET_MIGRATED: frozenset[str] = frozenset(
    {
        "src/server/chat/prompts.py",
        "src/vibesys/orchestration/dynamic/agents.py",
        "src/vibesys/orchestration/dynamic/orchestration.py",
        "src/vibesys/orchestration/dynamic/workstream.py",
        "src/vibesys/orchestration/evolve/agents.py",
        "src/vibesys/orchestration/evolve/orchestration.py",
        "src/vibesys/orchestration/issue_queue/agents.py",
        "src/vibesys/orchestration/issue_queue/prompts.py",
        "src/vibesys/orchestration/multi/agents.py",
        "src/vibesys/orchestration/multi/turns.py",
        "src/vibesys/orchestration/profiler_agent.py",
        "src/vibesys/orchestration/single/agents.py",
        "src/vibesys/orchestration/single/designer.py",
    }
)

_RENDERED_PROMPT_OWNER = "libs/vs-prompts/src/vs_prompts/rendered.py"
_STRING_METHODS = frozenset({"format", "join"})


@dataclass(frozen=True, order=True)
class _Violation:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


@dataclass(frozen=True)
class _Module:
    path: Path
    tree: ast.Module

    @property
    def relpath(self) -> str:
        return self.path.relative_to(_REPO_ROOT).as_posix()


def _source_roots() -> tuple[Path, ...]:
    return (_REPO_ROOT / "src", *sorted((_REPO_ROOT / "libs").glob("*/src")))


@cache
def _load(path: Path) -> _Module:
    return _Module(path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))


@cache
def _module_path(dotted: str) -> Path | None:
    relative = Path(*dotted.split("."))
    for root in _source_roots():
        for candidate in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
            if candidate.is_file():
                return candidate
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


@cache
def _parent_map(path: Path) -> dict[ast.AST, ast.AST]:
    return _parents(_load(path).tree)


def _enclosing_function(path: Path, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    parents = _parent_map(path)
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current
        current = parents.get(current)
    return None


def _resolve_function(
    path: Path, name: str, seen: frozenset[tuple[Path, str]] = frozenset()
) -> tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef] | None:
    """Find the first-party function ``name`` refers to at module level of ``path``."""
    if (path, name) in seen:
        return None
    tree = _load(path).tree
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return path, node
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                if (alias.asname or alias.name) == name and (target := _module_path(node.module)):
                    return _resolve_function(target, alias.name, seen | {(path, name)})
    return None


def _assignments(scope: ast.AST, name: str) -> tuple[list[ast.expr], list[ast.AugAssign]]:
    values: list[ast.expr] = []
    augmented: list[ast.AugAssign] = []
    nodes = ast.walk(scope) if not isinstance(scope, ast.Module) else iter(scope.body)
    for node in nodes:
        match node:
            case ast.Assign(targets=targets, value=value) if any(
                isinstance(t, ast.Name) and t.id == name for t in targets
            ):
                values.append(value)
            case ast.AnnAssign(target=ast.Name(id=target), value=ast.expr() as value) if (
                target == name
            ):
                values.append(value)
            case ast.AugAssign(target=ast.Name(id=target)) if target == name:
                augmented.append(node)
            case _:
                pass
    return values, augmented


class _SinkTracer:
    """Follows a prompt-sink expression back to where its text is produced."""

    def __init__(self) -> None:
        self.violations: set[_Violation] = set()
        self._visited: set[tuple[Path, int, int]] = set()

    def _report(self, path: Path, node: ast.AST, what: str) -> None:
        self.violations.add(_Violation(_load(path).relpath, getattr(node, "lineno", 0), what))

    def trace(self, path: Path, expr: ast.expr) -> None:
        key = (path, expr.lineno, expr.col_offset)
        if key in self._visited:
            return
        self._visited.add(key)
        match expr:
            case ast.Constant(value=str()):
                self._report(path, expr, "prompt text is a Python string literal")
            case ast.JoinedStr():
                self._report(path, expr, "prompt built with an f-string")
            case ast.BinOp(op=ast.Add() | ast.Mod()):
                self._report(path, expr, "prompt built with + or %")
            case ast.Call(func=ast.Attribute(attr=attr)) if attr in _STRING_METHODS:
                self._report(path, expr, f"prompt built with .{attr}")
            case ast.Call(func=ast.Name(id=name)):
                self._trace_function(path, name)
            case ast.Await(value=value):
                self.trace(path, value)
            case ast.IfExp(body=body, orelse=orelse):
                self.trace(path, body)
                self.trace(path, orelse)
            case ast.Name(id=name):
                self._trace_name(path, expr, name)
            case _:
                pass

    def _trace_function(self, path: Path, name: str) -> None:
        resolved = _resolve_function(path, name)
        if resolved is None:
            return
        target, function = resolved
        for node in ast.walk(function):
            if isinstance(node, ast.Return) and node.value is not None:
                self.trace(target, node.value)

    def _trace_name(self, path: Path, expr: ast.Name, name: str) -> None:
        function = _enclosing_function(path, expr)
        scope: ast.AST = function if function is not None else _load(path).tree
        values, augmented = _assignments(scope, name)
        if function is not None and not values:
            values, augmented = _assignments(_load(path).tree, name)
        for node in augmented:
            self._report(path, node, f"prompt variable {name!r} extended with +=")
        for value in values:
            self.trace(path, value)


def _prompt_sinks(tree: ast.Module) -> list[ast.expr]:
    sinks: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "turn":
            sinks.extend(node.args[:1])
            sinks.extend(k.value for k in node.keywords if k.arg == "message")
        sinks.extend(k.value for k in node.keywords if k.arg == "system_prompt")
    return sinks


def _is_render_call(node: ast.expr) -> bool:
    if isinstance(node, ast.Await):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else ""
    )
    return name.startswith("render")


def _rendered_output_edits(module: _Module) -> set[_Violation]:
    """``+``, ``+=``, f-string, ``.format`` or ``.join`` applied to a ``render*`` result."""
    found: set[_Violation] = set()
    parents = _parent_map(module.path)
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.expr):
            continue
        rendered = _is_render_call(node)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            function = _enclosing_function(module.path, node)
            values, _ = _assignments(function or module.tree, node.id)
            rendered = any(_is_render_call(value) for value in values)
        if not rendered:
            continue
        value = parents[node] if isinstance(parents.get(node), ast.Await) else node
        parent = parents.get(value)
        edited = (
            (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add | ast.Mod))
            or isinstance(parent, ast.FormattedValue | ast.AugAssign)
            or (isinstance(parent, ast.Attribute) and parent.attr in _STRING_METHODS)
            or (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Attribute)
                and parent.func.attr in _STRING_METHODS
            )
            or (isinstance(parent, ast.List | ast.Tuple) and _joined(parents.get(parent)))
        )
        if edited:
            found.add(_Violation(module.relpath, node.lineno, "rendered prompt edited in Python"))
    return found


def _formatted_constants(module: _Module) -> set[_Violation]:
    """``.format`` on a module-level string constant: template text kept in Python."""
    constants = {
        target.id
        for node in module.tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    return {
        _Violation(
            module.relpath, node.lineno, f"string constant {node.func.value.id} formatted in Python"
        )
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in constants
    }


def _joined(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _STRING_METHODS
    )


@cache
def _violations() -> frozenset[_Violation]:
    tracer = _SinkTracer()
    found: set[_Violation] = set()
    for path in sorted((_REPO_ROOT / "src").rglob("*.py")):
        module = _load(path)
        for sink in _prompt_sinks(module.tree):
            tracer.trace(path, sink)
        found |= _rendered_output_edits(module) | _formatted_constants(module)
    return frozenset(found | tracer.violations)


def test_prompts_in_migrated_modules_are_rendered_from_templates() -> None:
    unmigrated = sorted(v for v in _violations() if v.path not in _NOT_YET_MIGRATED)

    assert [str(v) for v in unmigrated] == []


@pytest.mark.parametrize("path", sorted(_NOT_YET_MIGRATED))
def test_not_yet_migrated_list_only_names_modules_with_violations(path: str) -> None:
    assert any(v.path == path for v in _violations()), (
        f"{path} builds no prompt in Python any more; remove it from _NOT_YET_MIGRATED"
    )


def test_only_the_renderer_constructs_rendered_prompts() -> None:
    constructions = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}"
        for root in _source_roots()
        for path in sorted(root.rglob("*.py"))
        if path.relative_to(_REPO_ROOT).as_posix() != _RENDERED_PROMPT_OWNER
        for node in ast.walk(_load(path).tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name | ast.Attribute)
        and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
        == "RenderedPrompt"
    ]

    assert constructions == []
