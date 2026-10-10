"""A test body awaits an event or queue arrival only with the producer that can end the wait.

``await gate.entered.wait()`` and ``await queue.get()`` in a test function hang
forever when the task that should produce the arrival fails or returns first.
Use ``tests.support.started_operation.arrival(<awaitable>, <task>)``, which raises
the task's own outcome instead.

The check is syntactic: a zero-argument ``await <x>.wait()`` or ``await <x>.get()``
made directly in the body of an ``async def test_*`` (not in a nested function).
Remaining sites are the ones whose producer is a task owned inside the code under
test, with no handle in the test; counts are exact so the list only shrinks.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "tests", *sorted((REPO / "libs").glob("*/tests")))
NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# Tracked in #1618: each needs the fake to expose its operation, then `arrival`.
_OWNED = "the producer is a task owned inside the code under test; the test holds no handle"

ALLOWED: dict[str, tuple[int, str]] = {
    "tests/vibesys/api/test_runs.py": (
        1,
        _OWNED,
    ),
    "tests/vibesys/run/test_evaluation_backend.py": (
        2,
        _OWNED,
    ),
    "tests/vibesys/run/test_evaluation_measurement_identity.py": (
        2,
        _OWNED,
    ),
    "tests/vibesys/run/test_evaluation_report_history.py": (
        1,
        _OWNED,
    ),
    "tests/vibesys/run/test_evidence_producer_contract.py": (
        1,
        _OWNED,
    ),
    "tests/vibesys/run/test_evidence_producer_failures.py": (
        1,
        _OWNED,
    ),
    "libs/vs-async-ops/tests/test_lifecycle.py": (
        6,
        _OWNED,
    ),
    "libs/vs-evaluation/tests/test_agent_service.py": (
        2,
        _OWNED,
    ),
    "libs/vs-runtime/tests/test_runs_contract.py": (
        3,
        _OWNED,
    ),
}


def _own_nodes(function: ast.AsyncFunctionDef) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, NESTED):
            continue
        nodes.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def bare_arrival_awaits(source: str) -> list[int]:
    """Line numbers of bare ``await x.wait()`` / ``await x.get()`` in async test bodies."""
    return sorted(
        node.lineno
        for function in ast.walk(ast.parse(source))
        if isinstance(function, ast.AsyncFunctionDef) and function.name.startswith("test_")
        for node in _own_nodes(function)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr in {"wait", "get"}
        and not node.value.args
        and not node.value.keywords
    )


def test_the_checker_flags_a_bare_arrival_await() -> None:
    source = "async def test_a(gate, q):\n    await gate.entered.wait()\n    await q.get()\n"
    assert bare_arrival_awaits(source) == [2, 3]


def test_the_checker_ignores_wrapped_workers_and_other_calls() -> None:
    source = (
        "async def test_a(gate, task):\n"
        "    await arrival(gate.entered.wait(), task)\n"
        "    async def worker():\n"
        "        await gate.wait()\n"
        "    await gate.wait(1)\n"
        "async def helper(gate):\n"
        "    await gate.wait()\n"
    )
    assert bare_arrival_awaits(source) == []


def test_no_new_bare_arrival_awaits_in_tests() -> None:
    found: dict[str, int] = {}
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            count = len(bare_arrival_awaits(path.read_text(encoding="utf-8")))
            if count:
                found[path.relative_to(REPO).as_posix()] = count
    allowed = {path: count for path, (count, _reason) in ALLOWED.items()}
    assert found == allowed, (
        "an awaited arrival hangs when its producer ends first; use "
        "tests.support.started_operation.arrival(<awaitable>, <task>), and lower the "
        f"ALLOWED count of any site you converted. found: {found}"
    )
