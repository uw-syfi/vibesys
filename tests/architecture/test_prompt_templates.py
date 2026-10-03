"""Architecture contract: agent prompts are rendered from templates.

Python passes data; templates own the wording, conditionals, and loops. This
check walks every prompt sink in ``src/`` (the message of an agent ``.turn``
call, every ``system_prompt=`` argument, every argument bound to a parameter
annotated ``RenderedPrompt`` such as a progress-log section, and the return
value of every ``@<server>.tool()`` function) back through local variables,
module constants, and functions and methods defined in or imported from
first-party modules, and reports text assembled or edited in Python: string
literals, ``+``, ``+=``, ``%``, f-strings, ``.format``, ``.join``,
``.replace``, ``str()``, and ``dedent``. It also reports those operations
applied to a value returned by a ``render*`` call, ``.format`` on a
module-level string constant (template text kept in Python), and any minting
of a ``RenderedPrompt`` outside ``vs_prompts/renderer.py``.

Every module is enforced except those in ``_NOT_YET_MIGRATED``. That list only
shrinks: a listed module that no longer has a violation fails the check until
it is removed from the list.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from functools import cache, cached_property
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable

_REPO_ROOT = Path(__file__).resolve().parents[2]

_NOT_YET_MIGRATED: frozenset[str] = frozenset()

_RENDERED_PROMPT_OWNER = "libs/vs-prompts/src/vs_prompts/renderer.py"
_MINTING_NAMES = frozenset({"RenderedPrompt", "_RENDER_TOKEN"})
_STRING_METHODS = frozenset({"format", "join", "replace", "dedent"})
_STRING_FUNCTIONS = frozenset({"str", "dedent"})
_SELF_NAMES = frozenset({"self", "cls"})

_Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, order=True)
class _Violation:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


@dataclass
class _Module:
    path: Path
    relpath: str
    tree: ast.Module

    @cached_property
    def parents(self) -> dict[ast.AST, ast.AST]:
        return {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}

    def enclosing(
        self, node: ast.AST, kind: type[ast.AST] | tuple[type[ast.AST], ...]
    ) -> ast.AST | None:
        current = self.parents.get(node)
        while current is not None and not isinstance(current, kind):
            current = self.parents.get(current)
        return current

    def function_of(self, node: ast.AST) -> _Function | None:
        found = self.enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        return found if isinstance(found, _Function) else None

    def imported_module(self, name: str) -> str | None:
        """The dotted module bound to ``name`` by an import, if any."""
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname == name or (alias.asname is None and alias.name == name):
                        return alias.name
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    if (alias.asname or alias.name) == name:
                        return f"{node.module}.{alias.name}"
        return None


@dataclass
class _Scanner:
    """Finds prompt text built in Python under ``scan_root``.

    ``source_roots`` resolve first-party imports; paths are reported relative
    to ``report_base``.
    """

    scan_root: Path
    source_roots: tuple[Path, ...]
    report_base: Path
    violations: set[_Violation] = field(default_factory=set)
    _modules: dict[Path, _Module] = field(default_factory=dict)
    _visited: set[tuple[Path, int, int]] = field(default_factory=set)

    def load(self, path: Path) -> _Module:
        if path not in self._modules:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relpath = path.relative_to(self.report_base).as_posix()
            self._modules[path] = _Module(path, relpath, tree)
        return self._modules[path]

    def module_path(self, dotted: str) -> Path | None:
        relative = Path(*dotted.split("."))
        for root in self.source_roots:
            for candidate in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
                if candidate.is_file():
                    return candidate
        return None

    def run(self) -> frozenset[_Violation]:
        paths = sorted(self.scan_root.rglob("*.py"))
        typed = _typed_sinks(self.load(path) for path in paths)
        for path in paths:
            module = self.load(path)
            for sink in _prompt_sinks(module.tree, typed, module.relpath):
                self.trace(module, sink)
            self.violations |= _rendered_output_edits(module) | _formatted_constants(module)
        return frozenset(self.violations)

    def report(self, module: _Module, node: ast.AST, what: str) -> None:
        self.violations.add(_Violation(module.relpath, getattr(node, "lineno", 0), what))

    def trace(self, module: _Module, expr: ast.expr) -> None:
        key = (module.path, expr.lineno, expr.col_offset)
        if key in self._visited:
            return
        self._visited.add(key)
        if (built := _built_in_python(expr)) is not None:
            self.report(module, expr, f"prompt built in Python: {built}")
            return
        match expr:
            case ast.Call(func=ast.Name(id=name)):
                self.trace_function(module, name)
            case ast.Call(func=ast.Attribute(value=ast.Name(id=owner), attr=attr)):
                self.trace_method(module, expr, owner, attr)
            case ast.Await(value=value):
                self.trace(module, value)
            case ast.IfExp() | ast.BoolOp():
                for alternative in _alternatives(expr):
                    self.trace(module, alternative)
            case ast.Name(id=name):
                self.trace_name(module, expr, name)
            case ast.Attribute(value=ast.Name(id=owner), attr=attr) if owner in _SELF_NAMES:
                self.trace_self_attribute(module, expr, attr)
            case _:
                pass

    def trace_returns(self, module: _Module, function: _Function) -> None:
        for node in ast.walk(function):
            if isinstance(node, ast.Return) and node.value is not None:
                self.trace(module, node.value)

    def resolve_function(
        self, module: _Module, name: str, depth: int = 0
    ) -> tuple[_Module, _Function] | None:
        """The first-party module-level function ``name`` refers to in ``module``."""
        for node in module.tree.body:
            if isinstance(node, _Function) and node.name == name:
                return module, node
        dotted = module.imported_module(name)
        if dotted is None or depth > 8:
            return None
        parent, _, member = dotted.rpartition(".")
        target = self.module_path(parent) if parent else None
        if target is None:
            return None
        return self.resolve_function(self.load(target), member, depth + 1)

    def trace_function(self, module: _Module, name: str) -> None:
        if (resolved := self.resolve_function(module, name)) is not None:
            self.trace_returns(*resolved)

    def trace_method(self, module: _Module, call: ast.Call, owner: str, attr: str) -> None:
        if owner in _SELF_NAMES:
            cls = module.enclosing(call, ast.ClassDef)
            if isinstance(cls, ast.ClassDef):
                for node in cls.body:
                    if isinstance(node, _Function) and node.name == attr:
                        self.trace_returns(module, node)
            return
        dotted = module.imported_module(owner)
        target = self.module_path(dotted) if dotted else None
        if target is not None and (resolved := self.resolve_function(self.load(target), attr)):
            self.trace_returns(*resolved)

    def trace_self_attribute(self, module: _Module, expr: ast.Attribute, attr: str) -> None:
        cls = module.enclosing(expr, ast.ClassDef)
        if not isinstance(cls, ast.ClassDef):
            return
        for node in ast.walk(cls):
            targets = node.targets if isinstance(node, ast.Assign) else []
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == attr
                    and isinstance(target.value, ast.Name)
                    and target.value.id in _SELF_NAMES
                    and isinstance(node, ast.Assign)
                ):
                    self.trace(module, node.value)

    def trace_name(self, module: _Module, expr: ast.Name, name: str) -> None:
        function = module.function_of(expr)
        values, augmented = _assignments(function or module.tree, name)
        if function is not None and not values:
            values, augmented = _assignments(module.tree, name)
        for node in augmented:
            self.report(module, node, f"prompt variable {name!r} extended with +=")
        for value in values:
            self.trace(module, value)


def _alternatives(expr: ast.IfExp | ast.BoolOp) -> tuple[ast.expr, ...]:
    """The values a conditional or an ``and``/``or`` expression may evaluate to."""
    return (expr.body, expr.orelse) if isinstance(expr, ast.IfExp) else tuple(expr.values)


def _built_in_python(expr: ast.expr) -> str | None:
    """Name the string-building operation ``expr`` is, if any."""
    match expr:
        case ast.Constant(value=str()):
            return "string literal"
        case ast.JoinedStr():
            return "f-string"
        case ast.BinOp(op=ast.Add() | ast.Mod()):
            return "+ or %"
        case ast.Call(func=ast.Attribute(attr=attr)) if attr in _STRING_METHODS:
            return f".{attr}"
        case ast.Call(func=ast.Name(id=name)) if name in _STRING_FUNCTIONS:
            return f"{name}()"
        case _:
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


# A function name mapped to its ``RenderedPrompt`` parameters: (positional index, name).
# A private (underscore) name is keyed by its module too, so it only matches there.
_TypedSinks = dict[tuple[str, str], tuple[tuple[int, str], ...]]


def _typed_sinks(modules: Iterable[_Module]) -> _TypedSinks:
    """Functions whose parameters are annotated ``RenderedPrompt``: agent-visible text sinks."""
    sinks: _TypedSinks = {}
    for module in modules:
        for node in ast.walk(module.tree):
            if not isinstance(node, _Function):
                continue
            positional = [*node.args.posonlyargs, *node.args.args]
            offset = 1 if positional and positional[0].arg in _SELF_NAMES else 0
            params = [
                (index - offset, arg.arg)
                for index, arg in enumerate(positional)
                if _names_rendered_prompt(arg.annotation)
            ] + [
                (-1, arg.arg)
                for arg in node.args.kwonlyargs
                if _names_rendered_prompt(arg.annotation)
            ]
            if params:
                sinks[_sink_key(module.relpath, node.name)] = tuple(params)
    return sinks


def _sink_key(relpath: str, name: str) -> tuple[str, str]:
    return (relpath if name.startswith("_") else "", name)


def _names_rendered_prompt(annotation: ast.expr | None) -> bool:
    match annotation:
        case ast.Name(id="RenderedPrompt") | ast.Attribute(attr="RenderedPrompt"):
            return True
        case ast.Constant(value=str() as text):
            return text == "RenderedPrompt"
        case _:
            return False


def _prompt_sinks(
    tree: ast.Module, typed: _TypedSinks | None = None, relpath: str = ""
) -> list[ast.expr]:
    sinks: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, _Function) and any(_is_tool_decorator(d) for d in node.decorator_list):
            # An agent tool's return value is text the agent reads.
            sinks.extend(
                ret.value
                for ret in ast.walk(node)
                if isinstance(ret, ast.Return) and ret.value is not None
            )
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "turn":
            sinks.extend(node.args[:1])
            sinks.extend(k.value for k in node.keywords if k.arg == "message")
        sinks.extend(k.value for k in node.keywords if k.arg == "system_prompt")
        for index, name in (typed or {}).get(_sink_key(relpath, _call_name(node)), ()):
            if 0 <= index < len(node.args):
                sinks.append(node.args[index])
            sinks.extend(k.value for k in node.keywords if k.arg == name)
    return sinks


def _is_tool_decorator(decorator: ast.expr) -> bool:
    """``@server.tool()`` or ``@server.tool``: the function is an agent-facing tool."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return isinstance(target, ast.Attribute) and target.attr == "tool"


def _call_name(node: ast.AST | None) -> str:
    if not isinstance(node, ast.Call):
        return ""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _is_render_call(node: ast.expr) -> bool:
    return _call_name(node.value if isinstance(node, ast.Await) else node).startswith("render")


def _edits_operand(parent: ast.AST | None, grandparent: ast.AST | None) -> bool:
    """Whether ``parent`` applies a string-building operation to its child."""
    match parent:
        case ast.BinOp(op=ast.Add() | ast.Mod()) | ast.FormattedValue() | ast.AugAssign():
            return True
        case ast.Attribute(attr=attr):
            return attr in _STRING_METHODS
        case ast.Call():
            return _call_name(parent) in _STRING_METHODS | _STRING_FUNCTIONS
        case ast.List() | ast.Tuple():
            return _call_name(grandparent) in _STRING_METHODS
        case _:
            return False


def _rendered_output_edits(module: _Module) -> set[_Violation]:
    """String-building operations applied to a ``render*`` result."""
    found: set[_Violation] = set()
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.expr):
            continue
        rendered = _is_render_call(node)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            values, _ = _assignments(module.function_of(node) or module.tree, node.id)
            rendered = any(_is_render_call(value) for value in values)
        if not rendered:
            continue
        value = module.parents[node] if isinstance(module.parents.get(node), ast.Await) else node
        parent = module.parents.get(value)
        if _edits_operand(parent, module.parents.get(parent) if parent else None):
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
        _Violation(module.relpath, node.lineno, "string constant formatted in Python")
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in constants
    }


@cache
def _repo_violations() -> frozenset[_Violation]:
    """Scan the repository once per process, on first use rather than at import."""
    source_roots = (_REPO_ROOT / "src", *sorted((_REPO_ROOT / "libs").glob("*/src")))
    return _Scanner(_REPO_ROOT / "src", source_roots, _REPO_ROOT).run()


def test_prompts_in_migrated_modules_are_rendered_from_templates() -> None:
    unmigrated = sorted(v for v in _repo_violations() if v.path not in _NOT_YET_MIGRATED)

    assert [str(v) for v in unmigrated] == []


@pytest.mark.parametrize("path", sorted(_NOT_YET_MIGRATED))
def test_not_yet_migrated_list_only_names_modules_with_violations(path: str) -> None:
    assert any(v.path == path for v in _repo_violations()), (
        f"{path} builds no prompt in Python any more; remove it from _NOT_YET_MIGRATED"
    )


def _minting_sites(path: Path, *, calls: bool) -> list[int]:
    """Lines that use the private mint token, or construct the type when ``calls``.

    Tests may call the constructor to prove it rejects a foreign token.
    """
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        match node:
            case ast.Call(
                func=ast.Name(id="RenderedPrompt") | ast.Attribute(attr="RenderedPrompt")
            ) if calls:
                lines.append(node.lineno)
            case ast.Name(id="_RENDER_TOKEN") | ast.Attribute(attr="_RENDER_TOKEN"):
                lines.append(node.lineno)
            case ast.ImportFrom(module=module) if module and module.startswith("vs_prompts"):
                if any(alias.name in _MINTING_NAMES - {"RenderedPrompt"} for alias in node.names):
                    lines.append(node.lineno)
            case _:
                pass
    return lines


def test_only_the_renderer_mints_rendered_prompts() -> None:
    libs = _REPO_ROOT / "libs"
    sources = (_REPO_ROOT / "src", *sorted(libs.glob("*/src")))
    tests = (_REPO_ROOT / "tests", *sorted(libs.glob("*/tests")))
    sites = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{line}"
        for roots, calls in ((sources, True), (tests, False))
        for root in roots
        for path in sorted(root.rglob("*.py"))
        if path.relative_to(_REPO_ROOT).as_posix() != _RENDERED_PROMPT_OWNER
        for line in _minting_sites(path, calls=calls)
    ]

    assert sites == []


_REPORTED = {
    "plus": "def f(session, value):\n    return session.turn('a' + value)",
    "augmented": (
        "def f(session, value):\n    message = render_x()\n    message += value\n"
        "    return session.turn(message)"
    ),
    "f-string": "def f(session, value):\n    return session.turn(f'do {value}')",
    "format": "def f(session, value):\n    return session.turn('do {}'.format(value))",
    "join": "def f(session, value):\n    return session.turn(' '.join([value, value]))",
    "percent": "def f(session, value):\n    return session.turn('do %s' % value)",
    "replace": "def f(session, r):\n    return session.turn(r.render_template('t.j2').replace('a', 'b'))",
    "str": "def f(session, value):\n    return session.turn(str(value))",
    "dedent": "def f(session, value):\n    return session.turn(textwrap.dedent(value))",
    "literal": "def f(session):\n    return session.turn('do the thing')",
    "system prompt literal": "ROLE = Role(system_prompt='You are a judge.')",
    "system prompt constant": "_PROMPT = 'You are a judge.'\nROLE = Role(system_prompt=_PROMPT)",
    "helper function": (
        "def _helper(value):\n    return f'do {value}'\n\n"
        "def f(session, value):\n    return session.turn(_helper(value))"
    ),
    "method": (
        "class Agent:\n    def _build(self, value):\n        return 'do ' + value\n\n"
        "    def run(self, session, value):\n        return session.turn(self._build(value))"
    ),
    "rendered edited": "def f(value):\n    text = render_x()\n    return text + value",
    "constant formatted": "_T = 'do {v}'\n\ndef f(value):\n    return render_x(extra=_T.format(v=value))",
    "typed sink": (
        "class Log:\n    def append(self, n: int, section: RenderedPrompt) -> None: ...\n\n"
        "def f(log, n):\n    log.append(n, f'## Round {n}')"
    ),
    "tool result": (
        "@server.tool()\ndef get(issue_id: int) -> str:\n    return f'(no issue #{issue_id})'"
    ),
    "or fallback": "def f(session, value):\n    return session.turn(value or 'nothing')",
    "private typed sink": (
        "def _write(path, text: RenderedPrompt) -> None: ...\n\n"
        "def f(path, value):\n    _write(path, 'x' + value)"
    ),
    "typed sink keyword": (
        "def write(*, text: RenderedPrompt) -> None: ...\n\n"
        "def f(value):\n    write(text='do ' + value)"
    ),
}

_ACCEPTED = {
    "render call": "def f(session, r, v):\n    return session.turn(r.render_template('t.j2', v=v))",
    "rendered variable": (
        "def f(session, v):\n    message = render_x(v=v)\n    return session.turn(message)"
    ),
    "parameter": "def f(session, value):\n    return session.turn(value)",
    "attribute": "def f(config):\n    return Role(system_prompt=config.system_prompt)",
    "rendering helper": (
        "def _helper(r, v):\n    return r.render_template('t.j2', v=v)\n\n"
        "def f(session, r, v):\n    return session.turn(_helper(r, v))"
    ),
    "data formatting into a render": "def f(n):\n    return render_x(round_id=f'{n:04d}')",
    "tool result rendered": (
        "@server.tool()\ndef get(r, issue_id: int) -> str:\n"
        "    return r.render_template('t.j2', issue_id=issue_id)"
    ),
    "typed sink rendered": (
        "class Log:\n    def append(self, n: int, section: RenderedPrompt) -> None: ...\n\n"
        "def f(log, r, n):\n    log.append(f'{n}', r.render_template('s.j2', n=n))"
    ),
}


def _scan_snippet(tmp_path: Path, source: str) -> frozenset[_Violation]:
    path = tmp_path / "src" / "pkg" / "mod.py"
    path.parent.mkdir(parents=True)
    path.write_text(source + "\n", encoding="utf-8")
    return _Scanner(tmp_path / "src", (tmp_path / "src",), tmp_path).run()


@pytest.mark.parametrize("source", _REPORTED.values(), ids=_REPORTED.keys())
def test_check_reports_prompt_text_built_in_python(tmp_path: Path, source: str) -> None:
    assert _scan_snippet(tmp_path, source)


@pytest.mark.parametrize("source", _ACCEPTED.values(), ids=_ACCEPTED.keys())
def test_check_accepts_rendered_prompts_and_data(tmp_path: Path, source: str) -> None:
    assert _scan_snippet(tmp_path, source) == frozenset()
