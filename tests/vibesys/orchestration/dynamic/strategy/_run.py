"""Run `DynamicStrategy` on the real core with scripted executors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._shell import Run, Script, drive_shell

from vibesys.orchestration.dynamic.core_policy.api import (
    reply_schemas,
    requirements_for,
)
from vibesys.orchestration.dynamic.strategy.api import (
    DynamicConfig,
    DynamicStrategy,
    DynamicStrategyState,
    dynamic_operation_registry,
)
from vs_core.api import ArtifactId, ArtifactRef, Limits, RevisionRef, RunEnvelope, RunFacts
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


def run_shell(
    executors: Script,
    *,
    limits: Limits | None = None,
    live: bool = True,
    max_concurrent: int = 1,
    **overrides: object,
) -> Run:
    """Run a fresh strategy to the end of its run on the production shell.

    The run must satisfy the liveness invariants (`vs_core.testing.liveness`) unless ``live``
    is False.
    """
    settings = config(**overrides)
    harness = Harness(
        registry=dynamic_operation_registry(),
        facts=FACTS,
        limits=limits or LIMITS,
        envelope_type=RunEnvelope[DynamicStrategyState],
        requirements=requirements_for(settings),
    )
    finished = drive_shell(
        DynamicStrategy(config=settings),
        executors,
        harness,
        reply_schemas(settings),
        max_concurrent=max_concurrent,
    )
    if live:
        assert_live(finished.journal, finished.core, Budget(retries=harness.limits.max_retries))
    if finished.halted is not None:
        raise finished.halted
    return finished


def kinds(trace: Trace[DynamicStrategyState] | Run) -> list[str]:
    return [type(item).__name__ for item in trace.decisions]
