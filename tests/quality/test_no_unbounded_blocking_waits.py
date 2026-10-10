"""A test never blocks without a bound on a thread, process or socket it does not control.

``thread.join()``, ``process.communicate()``, ``process.wait()`` and
``listener.accept()`` park the whole worker when the peer never finishes, and the
failure names nothing. Use ``tests.support.bounded_waits`` (``join_or_fail``,
``stop_process``, ``HANG_GUARD_S``) or pass ``timeout=``. The bound is a hang
guard that a passing run never approaches, not a synchronization tool.

The checks are syntactic: a zero-argument ``.join()`` or ``.communicate()``
(``str.join`` always takes an argument), a zero-argument ``.wait()`` on a name
bound to ``subprocess.Popen`` in the same function, and ``.accept()`` on a
socket-like name with no ``settimeout`` on it in the same function.
"""

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "tests", *sorted((REPO / "libs").glob("*/tests")))
SOCKET_NAME = re.compile(r"(server|listener|sock|socket)\w*", re.IGNORECASE)

# Files whose subject is this check, or that hold none of it as test code.
EXEMPT = {"tests/quality/test_no_unbounded_blocking_waits.py"}


def _is_popen(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "Popen"
    )


def _bound_names(function: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to a ``Popen`` and names given a ``settimeout``, in *function*."""
    popens: set[str] = set()
    timed: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and _is_popen(node.value):
            popens.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if _is_popen(item.context_expr) and isinstance(item.optional_vars, ast.Name):
                    popens.add(item.optional_vars.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "settimeout"
            and isinstance(node.func.value, ast.Name)
        ):
            timed.add(node.func.value.id)
    return popens, timed


def _unbounded(function: ast.AST) -> list[tuple[int, str]]:
    popens, timed = _bound_names(function)
    found: list[tuple[int, str]] = []
    for node in ast.walk(function):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.args or node.keywords:
            continue
        attr, receiver = node.func.attr, node.func.value
        name = receiver.id if isinstance(receiver, ast.Name) else None
        if attr in {"join", "communicate"}:
            found.append((node.lineno, f".{attr}()"))
        elif attr == "wait" and name in popens:
            found.append((node.lineno, f"{name}.wait()"))
        elif attr == "accept" and name and SOCKET_NAME.fullmatch(name) and name not in timed:
            found.append((node.lineno, f"{name}.accept() without settimeout"))
    return found


def unbounded_waits(source: str) -> list[tuple[int, str]]:
    """``(line, what)`` for each unbounded blocking wait in *source*."""
    tree = ast.parse(source)
    functions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    # Module-level statements count as one scope; nested functions are scanned in
    # their own scope and again inside their parent, so deduplicate by line.
    return sorted({hit for scope in (tree, *functions) for hit in _unbounded(scope)})


def test_the_checker_flags_each_unbounded_wait() -> None:
    source = (
        "def f(t, server):\n"
        "    p = subprocess.Popen(['x'])\n"
        "    t.join()\n"
        "    p.communicate()\n"
        "    p.wait()\n"
        "    server.accept()\n"
    )
    assert [what for _line, what in unbounded_waits(source)] == [
        ".join()",
        ".communicate()",
        "p.wait()",
        "server.accept() without settimeout",
    ]


def test_the_checker_accepts_bounded_waits() -> None:
    source = (
        "def f(t, server):\n"
        "    p = subprocess.Popen(['x'])\n"
        "    server.settimeout(5)\n"
        "    t.join(5)\n"
        "    p.communicate(timeout=5)\n"
        "    p.wait(timeout=5)\n"
        "    server.accept()\n"
        "    ','.join(parts)\n"
        "    event.wait()\n"
    )
    assert unbounded_waits(source) == []


def test_no_test_blocks_without_a_bound_on_a_thread_process_or_socket() -> None:
    offenders = [
        f"{path.relative_to(REPO).as_posix()}:{line}: {what}"
        for root in SCAN_ROOTS
        for path in sorted(root.rglob("*.py"))
        if path.relative_to(REPO).as_posix() not in EXEMPT
        for line, what in unbounded_waits(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "use tests.support.bounded_waits (join_or_fail, stop_process, HANG_GUARD_S) "
        "or pass a timeout:\n" + "\n".join(offenders)
    )
