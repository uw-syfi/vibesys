"""Run `DynamicStrategy` on the real core with scripted executors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.core_policy.api import requirements_for
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import ArtifactId, ArtifactRef, Limits, RevisionRef, RunEnvelope, RunFacts
from vs_core.testing.drive import Faults, Harness, Trace, drive
from tests.vibesys.orchestration.dynamic.strategy._shell import Run, drive_shell

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
    **overrides: object,
) -> Trace[DynamicStrategyState]:
    """Drive a fresh strategy to quiescence against ``executors``."""
    harness = Harness(
        registry=dynamic_operation_registry(),
        facts=FACTS,
        limits=limits or LIMITS,
        envelope_type=RunEnvelope[DynamicStrategyState],
    )
    return drive(DynamicStrategy(config=config(**overrides)), executors, harness, faults)


def run_shell(
    executors: Executors, *, limits: Limits | None = None, **overrides: object
) -> Run:
    """Run a fresh strategy to the end of its run on the production shell."""
    settings = config(**overrides)
    harness = Harness(
        registry=dynamic_operation_registry(),
        facts=FACTS,
        limits=limits or LIMITS,
        envelope_type=RunEnvelope[DynamicStrategyState],
        requirements=requirements_for(settings),
    )
    return drive_shell(DynamicStrategy(config=settings), executors, harness)


def kinds(trace: Trace[DynamicStrategyState] | Run) -> list[str]:
    return [type(item).__name__ for item in trace.decisions]
