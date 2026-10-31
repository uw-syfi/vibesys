"""Executors record effects and observations only through the shared mechanisms.

Execution receipts (the begun marker and the sealed result) are written by
``ReceiptStore.run_once`` and observations by ``ObservationFactory``. A hand-rolled
path would skip the begun marker, which is exactly what makes "never started" a
provable answer to ``InspectRequest``, and would issue observation sequences that
core rejects. These checks fail when an executor grows such a path.
"""

from __future__ import annotations

import ast
from pathlib import Path

from vs_runtime.api import core

SOURCE = Path(core.__file__).parents[1]  # the package's own source, scanned below

# The only modules that may write execution records or observation rows: the store and
# the factory, plus the operation receipt wrapper that seals a result an inspection proved.
OWNERS = frozenset(
    {
        "_receipt_store.py",
        "_observation_factory.py",
        "_operation_receipts.py",
        # A Fake of the whole shell, not a receipt-backed executor.
        "_fake_core_execution.py",
        # The scripted shell test kit: it stands in for the executors, so it builds the
        # observations their translators would.
        "_shell_kit.py",
        # The Fake step runner stands in for a runner, so it builds the result a runner returns.
        "_fake_step_runner.py",
        # The job runners' shared core builds the result of a step, whose observation is the one
        # it ends with (sequence 0): a runner keeps no observation row, so the factory, which
        # numbers rows across retries, has nothing to number.
        "_job_step_run.py",
    }
)
RECORD_NAMES = frozenset({"ExecutionRecord", "ExecutionPhase"})
FAMILIES = frozenset({"executions", "observations"})


def _violations(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name) and node.id in RECORD_NAMES:
            found.append(f"{path.name}:{line} uses {node.id}")
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id == "Observation":
                found.append(f"{path.name}:{line} builds an Observation outside the factory")
            if isinstance(callee, ast.Attribute) and callee.attr in {"seal", "sealed"}:
                found.append(f"{path.name}:{line} calls .{callee.attr}( outside run_once")
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and first.value in FAMILIES:
                found.append(f"{path.name}:{line} writes the {first.value!r} family directly")
    return found


def test_only_the_store_and_the_factory_write_execution_records_and_observations() -> None:
    problems = [
        problem
        for path in sorted(SOURCE.glob("*.py"))
        if path.name not in OWNERS
        for problem in _violations(path)
    ]
    assert problems == []
