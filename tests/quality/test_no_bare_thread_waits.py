"""A test never parks a worker on ``to_thread(<event>.wait)`` without a peer that can end it.

``await asyncio.to_thread(event.wait)`` hangs forever when the operation that
should set the event fails or returns first, and ``asyncio.run`` then joins the
parked worker at shutdown. Tests that hold the operation as a task or future use
``vs_sim.api.testing.wait_until_started``, which ends with it.

Executor-owned setters use ``wait_until_executor_started``, which ends with the
evaluation. The allowlist holds the remaining sites, where the gate is a fake's
own and the creating test opens it in a ``finally``. Counts are exact so the list
can only shrink: converting a site means lowering its count.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "tests", *sorted((REPO / "libs").glob("*/tests")))
WAITING_METHODS = {"wait", "get"}

# A fake's own gate, set by the creating test in a `finally`, so it opens on every path.
_RELEASED_BY_FINALLY = "a fake's release gate, set in the creating test's finally block"

ALLOWED: dict[str, tuple[int, str]] = {
    "libs/vs-runtime/tests/test_agent_journal_concurrency.py": (
        1,
        "the turn's done callback puts a sentinel, so the read returns even if the turn fails",
    ),
    "libs/vs-runtime/tests/test_agent_sessions.py": (1, _RELEASED_BY_FINALLY),
    "libs/vs-runtime/tests/test_prepared_conversations.py": (1, _RELEASED_BY_FINALLY),
}


def bare_thread_waits(source: str) -> list[int]:
    """Line numbers of ``to_thread(<name>.wait)`` and ``to_thread(<name>.get)`` calls."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_thread"
        and node.args
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr in WAITING_METHODS
    ]


def test_the_checker_flags_a_thread_wait_on_an_event() -> None:
    source = "async def f(e):\n    await asyncio.to_thread(gate.started.wait)\n"
    assert bare_thread_waits(source) == [2]


def test_the_checker_ignores_other_thread_work() -> None:
    source = "async def f(e):\n    await asyncio.to_thread(run, e.wait)\n    await e.wait()\n"
    assert bare_thread_waits(source) == []


def test_no_new_bare_thread_waits_in_tests() -> None:
    found: dict[str, int] = {}
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            count = len(bare_thread_waits(path.read_text(encoding="utf-8")))
            if count:
                found[path.relative_to(REPO).as_posix()] = count
    allowed = {path: count for path, (count, _reason) in ALLOWED.items()}
    assert found == allowed, (
        "to_thread(<event>.wait) hangs when its setter ends first; use "
        "vs_sim.api.testing.wait_until_started with the operation, "
        "and lower the ALLOWED count of any site you converted"
    )
