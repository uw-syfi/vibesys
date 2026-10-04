"""The strategy on the real core: delivery faults never change what it decides.

Each run calls the real `step` and `project`. The executors are scripted, so a
test says what an executor saw, never what core concluded. Runs that reach the
intent ledger need #1319; until it merges they raise `KernelNotImplementedError`
from the ledger stub, which is why they are strict xfails.
"""

from __future__ import annotations

import pytest
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._run import FACTS, config, kinds, run

from vibesys.orchestration.dynamic.strategy.api import DynamicStrategy, dynamic_operation_registry
from vs_core.api import KernelNotImplementedError, MeasurementFailure
from vs_core.testing.drive import Failed, Faults, Harness, new_run

needs_ledger = pytest.mark.xfail(
    strict=True, raises=KernelNotImplementedError, reason="needs the intent ledger, #1319"
)

FAULTS = {
    "none": Faults(),
    "unknown_first": Faults(unknown_first=lambda _request: True),
    "retry_first": Faults(retry_first=lambda _request: True),
    "duplicate": Faults(duplicate=True),
    "reorder": Faults(reorder=True),
    "reload": Faults(reload=True),
    "all": Faults(
        unknown_first=lambda _request: True,
        retry_first=lambda _request: True,
        duplicate=True,
        reorder=True,
        reload=True,
    ),
}


def _refused_baseline() -> Executors:
    """The evaluator refuses the baseline workload, so measuring never succeeds."""
    return Executors(submit=lambda _request: Failed(MeasurementFailure.WORKLOAD))


def test_the_declaration_starts_up_on_the_real_core() -> None:
    """Startup validation accepts the strategy's own declaration and operation codec."""
    strategy = DynamicStrategy(config=config())
    core = new_run(strategy, Harness(registry=dynamic_operation_registry(), facts=FACTS))
    assert core.run.declaration == strategy.declaration


@needs_ledger
def test_a_refused_baseline_is_retried_within_budget_then_planning_goes_on() -> None:
    trace = run(_refused_baseline())
    assert kinds(trace)[:3] == ["Measure", "Measure", "Measure"]
    assert "RequestTurn" in kinds(trace)


@needs_ledger
@pytest.mark.parametrize("name", [key for key in FAULTS if key != "none"])
def test_delivery_faults_do_not_change_the_decisions(name: str) -> None:
    """Unknown, retried, duplicated, reordered and reloaded observations decide the same."""
    plain = run(_refused_baseline())
    faulty = run(_refused_baseline(), faults=FAULTS[name])
    assert kinds(faulty) == kinds(plain)
