"""Run `DynamicStrategy` on the real core with scripted executors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    Limits,
    RevisionRef,
    RunEnvelope,
    RunFacts,
)
from vs_core.testing.drive import Faults, Harness, Trace, drive
from vs_core.testing.liveness import Budget, End, Journal, assert_live

if TYPE_CHECKING:
    from tests.vibesys.orchestration.dynamic.strategy._executors import Executors


def config(**overrides: object) -> DynamicConfig:
    return DynamicConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_rounds": 1,
            "max_in_flight": 1,
            **overrides,
        }
    )


FACTS = RunFacts(
    objective="make it faster",
    baseline=RevisionRef.of_git_commit("abc123"),
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)


# Room for a planner turn, a few attempts and their implement, review and correction turns.
LIMITS = Limits(max_attempts=4, max_turns=40, max_parallel=2, max_retries=2, max_refunds=2)


def run(
    executors: Executors,
    *,
    faults: Faults | None = None,
    limits: Limits | None = None,
    live: bool = True,
    end: End = End.TERMINAL,
    **overrides: object,
) -> Trace[DynamicStrategyState]:
    """Drive a fresh strategy to quiescence against ``executors``.

    The run must satisfy the liveness invariants (`vs_core.testing.liveness`) unless ``live``
    is False. ``end`` says how the scenario ends the run; `End.CUT_SHORT` is for a scenario
    this driver cannot take to the end of the run (it answers no agent turn).
    """
    chosen = limits or LIMITS
    harness = Harness(
        registry=dynamic_operation_registry(),
        facts=FACTS,
        limits=chosen,
        envelope_type=RunEnvelope[DynamicStrategyState],
    )
    trace = drive(DynamicStrategy(config=config(**overrides)), executors, harness, faults)
    if live:
        assert_live(
            Journal.from_log(trace.log),
            trace.core,
            Budget(retries=chosen.max_retries),
            end,
        )
    return trace


def kinds(trace: Trace[DynamicStrategyState]) -> list[str]:
    return [type(item).__name__ for item in trace.decisions]
