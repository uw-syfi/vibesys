"""Executors record observations only through the factory, and write no execution record.

Observations are numbered by ``ObservationFactory``: a hand-rolled path would issue sequences
that core rejects. An execution record (the begun marker and the sealed result) is a schema the
retained-run import reads (``_retained_receipts``) and nothing writes: the step loop owns
whether an effect runs. These checks fail when an executor grows either path.
"""

from __future__ import annotations

import ast
from pathlib import Path

from vs_runtime.api import core

SOURCE = Path(core.__file__).parents[1]  # the package's own source, scanned below

# The only modules that may build observations, write observation rows or name the execution
# record: the factory, and the retained schemas.
OWNERS = frozenset(
    {
        "_retained_receipts.py",
        "_observation_factory.py",
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
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and first.value in FAMILIES:
                found.append(f"{path.name}:{line} writes the {first.value!r} family directly")
    return found


def test_only_the_factory_writes_observations_and_nothing_writes_execution_records() -> None:
    problems = [
        problem
        for path in sorted(SOURCE.glob("*.py"))
        if path.name not in OWNERS
        for problem in _violations(path)
    ]
    assert problems == []
