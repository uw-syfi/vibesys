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

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    RenderRoleArtifacts,
    dynamic_operation_registry,
)
from vs_core.api import (
    ControlId,
    ControlInput,
    KernelNotImplementedError,
    MeasurementFailure,
    Operation,
    RequestTurn,
    RunControlEvent,
    RunResultProposal,
    StartAttempt,
)
from vs_core.testing.drive import Failed, Faults, Harness, drive, new_run

needs_ledger = pytest.mark.xfail(
    strict=True, raises=KernelNotImplementedError, reason="needs the intent ledger, #1319"
)

FAULTS = {
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
@pytest.mark.parametrize("name", FAULTS)
def test_delivery_faults_do_not_change_the_decisions(name: str) -> None:
    """Unknown, retried, duplicated, reordered and reloaded observations decide the same."""
    plain = run(_refused_baseline())
    faulty = run(_refused_baseline(), faults=FAULTS[name])
    assert kinds(faulty) == kinds(plain)


@needs_ledger
def test_every_prompt_goes_through_a_render_operation() -> None:
    """Prompts are rendered by a declared operation before each turn is requested."""
    trace = run(_refused_baseline())
    renders = [
        item
        for item in trace.decisions
        if isinstance(item, Operation) and isinstance(item.request, RenderRoleArtifacts)
    ]
    turns = [item for item in trace.decisions if isinstance(item, RequestTurn)]
    assert renders
    assert len(renders) == len(turns)


@needs_ledger
def test_the_state_survives_a_codec_reload_after_every_step() -> None:
    """Reloading the whole envelope after each step yields the run an unbroken one reaches."""
    plain = run(_refused_baseline())
    reloaded = run(_refused_baseline(), faults=Faults(reload=True))
    assert reloaded.strategy.state == plain.strategy.state
    # Compare the written form: a decoded value drops private codec proofs that `==` sees.
    assert reloaded.core.model_dump_json() == plain.core.model_dump_json()


def test_a_stop_control_ends_the_run_without_starting_work() -> None:
    """The operator's stop is core's to enforce; the strategy starts nothing after it."""
    stop = RunControlEvent(
        control=ControlInput(control_id=ControlId(root="stop"), action="stop"),
        now_at=1.0,
        result=RunResultProposal(outcome="cancelled", reason="operator stop"),
    )
    harness = Harness(registry=dynamic_operation_registry(), facts=FACTS, events=(stop,))
    trace = drive(DynamicStrategy(config=config()), _refused_baseline(), harness)
    assert trace.finished
    assert not any(isinstance(item, StartAttempt) for item in trace.decisions)
