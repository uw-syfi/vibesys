"""Run `DynamicStrategy` on the real core with scripted executors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import ArtifactId, ArtifactRef, Limits, RevisionRef, RunFacts
from vs_core.testing.drive import Faults, Harness, Trace, drive

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
        limits=limits or Limits(),
    )
    return drive(DynamicStrategy(config=config(**overrides)), executors, harness, faults)


def kinds(trace: Trace[DynamicStrategyState]) -> list[str]:
    return [type(item).__name__ for item in trace.decisions]
