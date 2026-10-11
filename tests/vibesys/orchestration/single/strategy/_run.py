"""Run `SingleStrategy` on the real core with scripted executors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.single.core_policy.api import reply_schemas, requirements_for
from vibesys.orchestration.single.strategy.api import (
    SingleConfig,
    SingleStrategy,
    SingleStrategyState,
    single_operation_registry,
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


def config(**overrides: object) -> SingleConfig:
    return SingleConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_rounds": 2,
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

LIMITS = Limits(max_attempts=4, max_turns=60, max_parallel=1, max_retries=2, max_refunds=4)


def harness(
    settings: SingleConfig,
    *,
    limits: Limits | None = None,
    lifecycle: frozenset[LifecycleCapability] = frozenset(),
) -> Harness:
    return Harness(
        registry=single_operation_registry(),
        facts=FACTS,
        limits=limits or LIMITS,
        envelope_type=RunEnvelope[SingleStrategyState],
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
    options = host or HostOptions()
    chosen = harness(settings, limits=limits, lifecycle=options.lifecycle)
    finished = drive_shell(
        SingleStrategy(config=settings), executors, chosen, reply_schemas(), options
    )
    if live:
        assert_live(finished.journal, finished.core, Budget(retries=chosen.limits.max_retries))
    if finished.halted is not None:
        raise finished.halted
    return finished


def kinds(trace: Run) -> list[str]:
    decisions: list[Decision] = trace.decisions
    return [type(item).__name__ for item in decisions]
