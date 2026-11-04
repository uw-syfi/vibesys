"""A close counts as done only once its effect returned: no latch or registry drop before it.

Three shapes report a release that may not have happened:

- **Latch first.** ``self._closed = True`` (or ``_released``) ahead of the call that closes,
  in the same function. When that call raises, the object reads as closed, and a retry of
  ``close`` returns at once, as if it had succeeded.
- **Registry drop in a finally.** ``try: <close> finally: registry.pop(key)``. When the close
  raises, the entry that names the resource is gone, so nothing can find it to close again.
- **A kept ``ExitStack``.** An object that holds a ``contextlib.ExitStack`` for its later
  close: ``ExitStack.close`` drops each step before running it, so a retried close runs
  nothing that failed the first time.

Use ``vs_sim.api.CloseOnce`` or ``AsyncCloseOnce`` over a ``ReleaseStack``: the close counts
only when its effect returns ``Released``, a step that raised runs again on the next call,
and a ``with ExitStack()`` that unwinds a failed construction stays fine.

``ALLOWED`` and ``KEPT_EXIT_STACKS`` hold the remaining sites with counts that only shrink:
converting a site means lowering its count.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.parsed_source import STATIC_SCAN_GROUP, parse_source

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.xdist_group(STATIC_SCAN_GROUP)

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "src", *sorted((REPO / "libs").glob("*/src")))
LATCH = re.compile(r"^_?(closed|released)$")
EFFECT = re.compile(
    r"(^|_)(a?close|release|stop|remove|terminate|kill|cancel|shutdown|unlink)(_|$)", re.IGNORECASE
)
"""A call whose name says it closes something: ``close``, ``aclose``, ``stop_sandbox``, ..."""
DROPS = frozenset({"pop", "discard", "remove"})

_IN_MEMORY = "the effect only changes in-memory state and cannot fail"
ALLOWED: dict[str, tuple[int, str]] = {
    "libs/vs-agent/src/vs_agent/scripted_provider.py": (1, _IN_MEMORY),
    "libs/vs-core/src/vs_core/flows/_run.py": (1, _IN_MEMORY),
    "libs/vs-runtime/src/vs_runtime/_workspaces.py": (
        1,
        "begin_close latches admission only; the close itself runs through AsyncCloseOnce",
    ),
    "libs/vs-sim/src/vs_sim/sim_network.py": (1, _IN_MEMORY),
    "libs/vs-sim/src/vs_sim/sim_threads.py": (1, _IN_MEMORY),
}
KEPT_EXIT_STACKS: dict[str, tuple[int, str]] = {}


def _callee(call: ast.Call) -> str:
    function = call.func
    if isinstance(function, ast.Attribute):
        return function.attr
    return function.id if isinstance(function, ast.Name) else ""


def _own_nodes(node: ast.AST) -> list[ast.AST]:
    """Every node under ``node``, without entering nested functions, lambdas or classes."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        found.append(child)
        stack.extend(ast.iter_child_nodes(child))
    return found


def _effects(nodes: list[ast.AST]) -> list[ast.Call]:
    return [n for n in nodes if isinstance(n, ast.Call) and EFFECT.search(_callee(n))]


def _is_latch(node: ast.Assign) -> bool:
    if len(node.targets) != 1:
        return False
    target = node.targets[0]
    return (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
        and LATCH.match(target.attr) is not None
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    )


def _drops(nodes: list[ast.AST]) -> bool:
    for node in nodes:
        if isinstance(node, ast.Call) and _callee(node) in DROPS:
            return True
        if isinstance(node, ast.Delete) and any(
            isinstance(target, ast.Subscript) for target in node.targets
        ):
            return True
    return False


def _names_exit_stack(annotation: ast.expr | None) -> bool:
    return annotation is not None and any(
        isinstance(node, ast.Name | ast.Attribute)
        and (node.id if isinstance(node, ast.Name) else node.attr) == "ExitStack"
        for node in ast.walk(annotation)
    )


def _is_exit_stack_call(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Call) and _callee(value) == "ExitStack"


def _kept_by(method: ast.FunctionDef | ast.AsyncFunctionDef) -> list[int]:
    """Lines where ``method`` stores a new ``ExitStack``, or one it was given, on ``self``."""
    stacks = {
        argument.arg
        for argument in (*method.args.args, *method.args.kwonlyargs)
        if _names_exit_stack(argument.annotation)
    }
    return [
        node.lineno
        for node in _own_nodes(method)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            for target in node.targets
        )
        and (
            _is_exit_stack_call(node.value)
            or (isinstance(node.value, ast.Name) and node.value.id in stacks)
        )
    ]


def kept_exit_stacks(source: str) -> list[int]:
    """Line numbers where a class keeps an ``ExitStack`` for a later close.

    A field annotated ``ExitStack``, ``self.x = ExitStack()``, or ``self.x = <parameter
    annotated ExitStack>``.
    """
    if "ExitStack" not in source:
        return []
    lines: list[int] = []
    for node in ast.walk(parse_source(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for member in node.body:
            if isinstance(member, ast.AnnAssign) and _names_exit_stack(member.annotation):
                lines.append(member.lineno)
            elif isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                lines.extend(_kept_by(member))
    return sorted(lines)


def early_releases(source: str) -> list[int]:
    """Line numbers of latches set before a close's effect and of registry drops in a finally."""
    # A rule names what it looks for, so a source that never spells it cannot violate it.
    if "finally" not in source and "= True" not in source:
        return []
    lines: list[int] = []
    for node in ast.walk(parse_source(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            own = _own_nodes(node)
            effects = _effects(own)
            lines.extend(
                latch.lineno
                for latch in own
                if isinstance(latch, ast.Assign)
                and _is_latch(latch)
                and any(call.lineno > latch.lineno for call in effects)
            )
        elif isinstance(node, ast.Try) and node.finalbody:
            body = [n for statement in node.body for n in (statement, *_own_nodes(statement))]
            final = [n for statement in node.finalbody for n in (statement, *_own_nodes(statement))]
            if _effects(body) and _drops(final):
                lines.append(node.lineno)
    return sorted(lines)


def test_the_checker_flags_a_latch_set_before_the_close() -> None:
    source = (
        "class A:\n"
        "    def close(self):\n"
        "        if self._closed:\n"
        "            return\n"
        "        self._closed = True\n"
        "        self._sandbox.stop()\n"
    )
    assert early_releases(source) == [5]


def test_the_checker_flags_an_awaited_close_after_the_latch() -> None:
    source = "class A:\n    async def aclose(self):\n        self.released = True\n        await self._session.aclose()\n"
    assert early_releases(source) == [3]


def test_the_checker_accepts_a_latch_set_after_the_close_returned() -> None:
    source = (
        "class A:\n"
        "    def close(self):\n"
        "        self._sandbox.stop()\n"
        "        self._closed = True\n"
    )
    assert early_releases(source) == []


def test_the_checker_flags_a_registry_drop_in_a_finally_around_a_close() -> None:
    source = (
        "def close_one(registry, key):\n"
        "    try:\n"
        "        registry[key].close()\n"
        "    finally:\n"
        "        registry.pop(key, None)\n"
        "def drop_one(registry, key):\n"
        "    try:\n"
        "        await registry[key].release()\n"
        "    finally:\n"
        "        del registry[key]\n"
    )
    assert early_releases(source) == [2, 7]


def test_the_checker_ignores_a_finally_that_drops_without_closing_in_the_try() -> None:
    source = (
        "def serve(open_set, connection):\n"
        "    try:\n"
        "        connection.send(b'x')\n"
        "    finally:\n"
        "        open_set.discard(connection)\n"
        "        connection.close()\n"
    )
    assert early_releases(source) == []


def test_the_checker_flags_a_kept_exit_stack_and_not_a_scoped_one() -> None:
    source = (
        "@dataclass\n"
        "class A:\n"
        "    stack: ExitStack\n"
        "class B:\n"
        "    def __init__(self, ownership: ExitStack) -> None:\n"
        "        self._ownership = ownership\n"
        "        self._scopes = contextlib.ExitStack()\n"
        "def build():\n"
        "    with ExitStack() as stack:\n"
        "        stack.callback(print)\n"
    )
    assert kept_exit_stacks(source) == [3, 6, 7]


def _scan(rule: Callable[[str], list[int]]) -> dict[str, int]:
    found: dict[str, int] = {}
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            count = len(rule(path.read_text(encoding="utf-8")))
            if count:
                found[path.relative_to(REPO).as_posix()] = count
    return found


def test_no_kept_exit_stack_for_a_close() -> None:
    allowed = {path: count for path, (count, _reason) in KEPT_EXIT_STACKS.items()}
    assert _scan(kept_exit_stacks) == allowed, (
        "an ExitStack kept for a later close forgets a step that failed, so a retried close "
        "reports released; keep a vs_sim.api.ReleaseStack and close it through CloseOnce, "
        "and lower the KEPT_EXIT_STACKS count of any site you converted"
    )


def test_no_new_close_latch_before_its_effect() -> None:
    allowed = {path: count for path, (count, _reason) in ALLOWED.items()}
    assert _scan(early_releases) == allowed, (
        "a closed or released flag set before the close's effect, or a registry entry "
        "dropped in a finally around a close, reports a release that may not have "
        "happened; use vs_sim.api.CloseOnce or AsyncCloseOnce, and lower the ALLOWED "
        "count of any site you converted"
    )
