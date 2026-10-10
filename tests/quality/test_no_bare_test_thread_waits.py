"""A test body never parks its own thread on ``<event>.wait()`` for a worker's progress.

A bare ``started.wait()`` in a test function hangs forever when the worker that
should set the event fails or returns first. Use
``tests.support.started_operation.wait_until_started_sync`` with the worker's
future (``start_thread`` or a pool), which ends with it.

The check is syntactic: a zero-argument, non-awaited ``.wait()`` called directly
in the body of a ``test_*`` function (not in a nested function or lambda, which
run on a worker).
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "tests", *sorted((REPO / "libs").glob("*/tests")))
NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# Calls whose coroutine argument runs on the loop, where it is cancellable.
COROUTINE_WRAPPERS = {"create_task", "ensure_future", "gather", "wait_for", "shield", "arrival"}
ALLOWED: dict[str, tuple[int, str]] = {
    "libs/vs-slurm/tests/test_cluster_contract.py": (
        1,
        "the cancellation future's done callback sets the event, so it opens even if the cancel ends first",
    ),
    "tests/vibesys/skypilot/test_evaluator_helper.py": (
        1,
        "`ready` is set in the server thread's `finally`, so it opens even if the thread fails",
    ),
}


def _wrapper_name(call: ast.Call) -> str | None:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def _own_calls(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    """Calls in *function*'s own body that run on the test thread.

    Skips nested functions, lambdas and classes (they run on a worker), awaited calls
    (cancellable on the loop), and the arguments of coroutine wrappers such as
    ``create_task(x.wait())`` and ``arrival(x.wait(), task)``, which build a coroutine
    for the loop and park nothing.
    """
    calls: list[ast.Call] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, NESTED):
            continue
        awaited = isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
        call = node.value if awaited and isinstance(node, ast.Await) else node
        if isinstance(call, ast.Call):
            if _wrapper_name(call) in COROUTINE_WRAPPERS:
                continue
            if not awaited:
                calls.append(call)
            pending.extend(ast.iter_child_nodes(call))
            continue
        pending.extend(ast.iter_child_nodes(node))
    return calls


def bare_test_thread_waits(source: str) -> list[int]:
    """Line numbers of zero-argument ``.wait()`` calls made directly by a test function."""
    return sorted(
        call.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
        for call in _own_calls(node)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "wait"
        and not call.args
        and not call.keywords
    )


def test_the_checker_flags_a_wait_in_a_test_body() -> None:
    source = "def test_a(started):\n    started.wait()\n"
    assert bare_test_thread_waits(source) == [2]


def test_the_checker_ignores_workers_awaits_and_bounded_waits() -> None:
    source = (
        "async def test_a(gate, started):\n"
        "    def worker():\n"
        "        gate.wait()\n"
        "    await started.wait()\n"
        "    started.wait(timeout=1)\n"
        "    helper = lambda: gate.wait()\n"
        "def helper_not_a_test(gate):\n"
        "    gate.wait()\n"
    )
    assert bare_test_thread_waits(source) == []


def test_no_test_parks_its_own_thread_on_an_event() -> None:
    found: dict[str, int] = {}
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            count = len(bare_test_thread_waits(path.read_text(encoding="utf-8")))
            if count:
                found[path.relative_to(REPO).as_posix()] = count
    allowed = {path: count for path, (count, _reason) in ALLOWED.items()}
    assert found == allowed, (
        "a test-thread <event>.wait() hangs when its worker ends first; use "
        "tests.support.started_operation.wait_until_started_sync with the worker's future"
    )
