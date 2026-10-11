"""Run `IssueQueueStrategy` on the real core with scripted executors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.issue_queue.strategy._executors import WorldTranslator

from vibesys.orchestration.issue_queue.core_policy.api import (
    limits_for,
    reply_schemas,
    requirements_for,
)
from vibesys.orchestration.issue_queue.strategy.api import (
    IssueQueueConfig,
    IssueQueueStrategy,
    IssueQueueStrategyState,
    plain_operation_registry,
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
from vs_core.testing.drive import Faults, Harness, Trace, drive
from vs_core.testing.liveness import Budget, End, Journal, assert_live
from vs_runtime.api.testing import HostOptions, Run, Script, drive_shell

if TYPE_CHECKING:
    from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors


def config(**overrides: object) -> IssueQueueConfig:
    return IssueQueueConfig.model_validate(
        {
            "recipe": ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            "max_rounds": 2,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 2,
            "max_issues_per_round": 4,
            **overrides,
        }
    )


FACTS = RunFacts(
    objective="serve the model",
    baseline=RevisionRef.of_git_commit("abc123"),
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)


def limits(settings: IssueQueueConfig) -> Limits:
    return limits_for(
        settings, queue_allowance=900.0, observe_interval=10.0, observe_backoff_cap=120.0
    )


def harness(
    settings: IssueQueueConfig,
    *,
    deadline_at: float = Harness.deadline_at,
    lifecycle: frozenset[LifecycleCapability] = frozenset(),
) -> Harness:
    return Harness(
        registry=plain_operation_registry(),
        facts=FACTS,
        limits=limits(settings),
        envelope_type=RunEnvelope[IssueQueueStrategyState],
        requirements=requirements_for(),
        deadline_at=deadline_at,
        lifecycle=lifecycle,
    )


def run(
    executors: Executors,
    *,
    faults: Faults | None = None,
    live: bool = True,
    end: End = End.TERMINAL,
    **overrides: object,
) -> Trace[IssueQueueStrategyState]:
    """Drive a fresh strategy to quiescence against ``executors``."""
    settings = config(**overrides)
    chosen = harness(settings)
    trace = drive(IssueQueueStrategy(config=settings), executors, chosen, faults)
    if live:
        assert_live(
            Journal.from_log(trace.log),
            trace.core,
            Budget(retries=chosen.limits.max_retries),
            end,
        )
    return trace


def run_shell(
    executors: Script,
    *,
    live: bool = True,
    deadline_at: float = Harness.deadline_at,
    host: HostOptions | None = None,
    raise_halt: bool = True,
    **overrides: object,
) -> Run:
    """Run a fresh strategy to the end of its run on the production shell."""
    settings = config(**overrides)
    chosen_host = host or HostOptions(translator=WorldTranslator)
    chosen = harness(settings, deadline_at=deadline_at, lifecycle=chosen_host.lifecycle)
    finished = drive_shell(
        IssueQueueStrategy(config=settings),
        executors,
        chosen,
        reply_schemas(settings),
        chosen_host,
    )
    if live:
        assert_live(finished.journal, finished.core, Budget(retries=chosen.limits.max_retries))
    if finished.halted is not None and raise_halt:
        raise finished.halted
    return finished


def kinds(trace: Trace[IssueQueueStrategyState] | Run) -> list[str]:
    return [type(item).__name__ for item in trace.decisions]
