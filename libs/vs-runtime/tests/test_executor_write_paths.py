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
from typing import TYPE_CHECKING

import pytest
from tests.support.executor_cases import CASES
from tests.support.executor_context import RevocableLease

from vs_runtime.api import core
from vs_runtime.api.core import HAND_ROLLED_ROLES, REQUEST_DISPATCH, NeverBegun, ReceiptStore

if TYPE_CHECKING:
    from tests.support.executor_harness import ExecutorCase, Scenario

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


PARAMS = [
    pytest.param(
        case,
        scenario,
        id=f"{case.name}-{scenario.name}",
        marks=pytest.mark.xfail(
            REQUEST_DISPATCH[scenario.kind] in HAND_ROLLED_ROLES,
            reason="this role keeps its own receipts until it moves onto run_once (S4)",
            strict=True,
        ),
    )
    for case in CASES
    for scenario in case.scenarios
    if scenario.effectful
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "scenario"), PARAMS)
async def test_every_effectful_kind_leaves_an_execution_record(
    case: ExecutorCase, scenario: Scenario
) -> None:
    async with case.world() as world:
        request = await world.prepare(scenario)
        await world.execute(request, lease=RevocableLease(), crash_at=None)
        store = ReceiptStore(world.receipts_namespace())
        assert request.request_id is not None
        assert not isinstance(store.history(request.request_id.root), NeverBegun)
