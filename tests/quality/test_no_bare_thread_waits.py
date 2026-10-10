"""A test never parks a worker on ``to_thread(<event>.wait)`` without a peer that can end it.

``await asyncio.to_thread(event.wait)`` hangs forever when the operation that
should set the event fails or returns first, and ``asyncio.run`` then joins the
parked worker at shutdown. Tests that hold the operation as a task or future use
``tests.support.started_operation.wait_until_started``, which ends with it.

The allowlist holds the remaining sites, where the setter is a worker or task
owned inside the code under test and the test holds no handle to it. Counts are
exact so the list can only shrink: converting a site means lowering its count.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (REPO / "tests", *sorted((REPO / "libs").glob("*/tests")))
WAITING_METHODS = {"wait", "get"}

# The helper's own wait: it is released by the operation's done callback.
_HELPER = "tests/support/started_operation.py"
# The setter is a background task or thread that submit() starts inside the executor.
_INTERNAL_SETTER = "setter is a task or worker owned by the executor under test"

ALLOWED: dict[str, tuple[int, str]] = {
    _HELPER: (1, "released by the operation's done callback"),
    "tests/vibesys/run/test_slurm_evaluation.py": (2, _INTERNAL_SETTER),
    "libs/vs-sandbox/tests/test_slurm_executor.py": (10, _INTERNAL_SETTER),
    "libs/vs-sandbox/tests/test_slurm_lifecycle.py": (4, _INTERNAL_SETTER),
    "libs/vs-sandbox/tests/test_slurm_scheduler_contract.py": (6, _INTERNAL_SETTER),
    "libs/vs-sandbox/tests/test_slurm_admission_cancel.py": (2, _INTERNAL_SETTER),
    "libs/vs-sandbox/tests/test_slurm_cancel_in_flight.py": (1, _INTERNAL_SETTER),
    "libs/vs-runtime/tests/test_evaluation_executor_contract.py": (1, _INTERNAL_SETTER),
    "libs/vs-runtime/tests/test_agent_journal_concurrency.py": (
        1,
        "queue read whose setter is a ThreadPoolExecutor future, not an asyncio one",
    ),
    "libs/vs-runtime/tests/test_agent_sessions.py": (
        1,
        "a fake's own release gate, set by the test after the assertions",
    ),
    "libs/vs-runtime/tests/test_prepared_conversations.py": (
        1,
        "a fake's own release gate, set by the test after the assertions",
    ),
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
        "tests.support.started_operation.wait_until_started with the operation, "
        "and lower the ALLOWED count of any site you converted"
    )
