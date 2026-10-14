"""A request that runs for many lease durations does not cost the run its lease.

The loop renews the lease beside the requests it executes. A request that takes several lease
durations of run time (an agent turn) holds the loop at its await; without the heartbeat the
next commit is stamped after the lease lapsed and the run dies on a fence conflict. This runs
the production shell and loop on a virtual clock over a one-step search, so it costs nothing in
real time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_core.api import (
    AttemptBudget,
    AttemptId,
    EnsureWorkspace,
    Flow,
    FlowState,
    ItemId,
    OperationRegistry,
    ResourceId,
    RevisionRef,
    RunEnvelope,
    RunFacts,
    RunStatus,
    Value,
    WorkspaceMode,
    WorkspacePlan,
)
from vs_core.api.testing import Harness, Succeeded, sample_limits, sample_retries
from vs_core.flows import search
from vs_runtime.api.testing import LEASE, HostOptions, Launch, ScriptedExecutors, scripted_host
from vs_sim.api.testing import VirtualClock, examples

if TYPE_CHECKING:
    from vs_runtime.api.core import ExecutionContext, ExecutionResult

BASELINE = RevisionRef.of_git_commit("abc123")
FACTS = RunFacts(
    objective="open one workspace",
    baseline=BASELINE,
    evaluator_digest="evaluator",
    workload_digest="workload",
    environment_digest="environment",
)


class OneStepConfig(Value):
    """The one-step search takes no settings."""


@search("one-step", config=OneStepConfig)
async def one_step(ctx: Flow, config: OneStepConfig) -> None:
    """Open one workspace and leave; the run then ends on the input."""
    del config
    async with ctx.workspaces.open(
        attempt=AttemptId(root="attempt:0"),
        item=ItemId(root="item:0"),
        plan=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=ctx.facts.baseline),
        budget=AttemptBudget(),
    ):
        pass


def _answer(request: object, core: object) -> Succeeded:
    del core
    if isinstance(request, EnsureWorkspace):
        return Succeeded(resource_id=ResourceId(root="workspace:0"), revision=request.plan.base)
    return Succeeded()


@settings(max_examples=examples(pr=3, nightly=20), deadline=None, derandomize=True, database=None)
@given(thirds=st.integers(min_value=1, max_value=30))
def test_a_request_longer_than_the_lease_does_not_lose_it(thirds: int) -> None:
    """Run time of ``thirds`` third-lease steps passes inside one request; the run still ends."""
    clocks: list[VirtualClock] = []

    class LongExecutors(ScriptedExecutors):
        async def execute(self, request: object, context: ExecutionContext) -> ExecutionResult:
            if isinstance(request, EnsureWorkspace):
                for _ in range(thirds):
                    await clocks[0].sleep(LEASE / 3)
            return await super().execute(request, context)  # ty: ignore[invalid-argument-type]

    harness = Harness(
        registry=OperationRegistry(()),
        facts=FACTS,
        limits=sample_limits(
            max_attempts=2, max_turns=2, max_parallel=1, request_retries=sample_retries(2)
        ),
        envelope_type=RunEnvelope[FlowState],
    )
    host = scripted_host(
        Launch(one_step, OneStepConfig()),
        _answer,
        harness,
        {},
        HostOptions(translator=LongExecutors),
    )
    clocks.append(host.clock)

    run = host.run()

    assert run.halted is None
    assert run.core.run.status == RunStatus.TERMINAL
