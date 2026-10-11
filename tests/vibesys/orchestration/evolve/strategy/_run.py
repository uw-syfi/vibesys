"""Run `EvolveStrategy` on the real core with scripted executors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.evolve.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.evolve.strategy.api import (
    EvolveConfig,
    EvolveStrategy,
    EvolveStrategyState,
    evolve_operation_registry,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    LifecycleCapability,
    Limits,
    RevisionRef,
    RunEnvelope,
    RunFacts,
)
from vs_core.testing.drive import Harness
from vs_core.testing.liveness import Budget, assert_live
from vs_runtime.api.testing import HostOptions, Run, Script, drive_shell

if TYPE_CHECKING:
    from vs_core.api import Decision


def config(**overrides: object) -> EvolveConfig:
    return EvolveConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_generations": 2,
            "children_per_generation": 2,
            "k_top_inspirations": 1,
            "k_random_inspirations": 1,
            "selection_temperature": 1.0,
            "frontier_bias": 0.7,
            "bootstrap_max_attempts": 2,
            "seed": 7,
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


def limits_for_config(settings: EvolveConfig) -> Limits:
    """Generous bounds that still fit the run's own budgets."""
    attempts = settings.total_attempts
    return Limits(
        max_attempts=attempts,
        max_turns=60 * attempts,
        max_parallel=settings.max_parallelism,
        max_retries=4,
        max_refunds=attempts,
    )


def harness(
    settings: EvolveConfig,
    *,
    limits: Limits | None = None,
    lifecycle: frozenset[LifecycleCapability] = frozenset(),
) -> Harness:
    return Harness(
        registry=evolve_operation_registry(),
        facts=FACTS,
        limits=limits or limits_for_config(settings),
        envelope_type=RunEnvelope[EvolveStrategyState],
        requirements=requirements_for(settings),
        lifecycle=lifecycle,
    )


def run_shell(
    executors: Script,
    *,
    limits: Limits | None = None,
    live: bool = True,
    host: HostOptions | None = None,
    **overrides: object,
) -> Run:
    """Run a fresh strategy to the end of its run on the production shell."""
    settings = config(**overrides)
    options = host or HostOptions(max_concurrent=settings.max_parallelism)
    chosen = harness(settings, limits=limits, lifecycle=options.lifecycle)
    finished = drive_shell(
        EvolveStrategy(config=settings), executors, chosen, reply_schemas(), options
    )
    if live:
        assert_live(finished.journal, finished.core, Budget(retries=chosen.limits.max_retries))
    if finished.halted is not None:
        raise finished.halted
    return finished


def kinds(trace: Run) -> list[str]:
    decisions: list[Decision] = trace.decisions
    return [type(item).__name__ for item in decisions]
