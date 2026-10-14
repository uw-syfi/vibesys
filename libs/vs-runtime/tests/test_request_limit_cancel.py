"""Shell contract: a request that never finishes is cancelled at its limit and reported.

The real kernel runs. There is no real time: the clock is the ``now_at`` the test passes
and the executor waits on an event nobody sets, so the only thing that can end the
request is the loop's cancel at the limit it was given.
"""

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    AttemptBudget,
    AttemptId,
    ClockAdvanced,
    EnsureWorkspace,
    FailureReason,
    Flow,
    FlowDriver,
    FlowState,
    ItemId,
    NotOpened,
    OperationRegistry,
    Request,
    RequestPhase,
    Value,
    Within,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
)
from vs_core.api.testing import Harness, new_run
from vs_core.flows import search
from vs_project.api import FakeStateStore
from vs_runtime.api.core import (
    CoreRuntime,
    CoreRuntimeBindings,
    ExecutionContext,
    ExecutionOutcome,
    RequestExecutors,
)
from vs_runtime.api.testing import FakePublicationDelivery

LIMIT = 10.0


class Config(Value):
    """The search has nothing to configure."""


class Heard(Value):
    """Why the workspace did not open, as the flow heard it."""

    reasons: tuple[FailureReason, ...] = ()


@search("one-workspace", config=Config)
async def one_workspace(ctx: Flow, config: Config) -> None:
    """Asks for one workspace with a limit and publishes why it did not open."""
    del config
    plan = WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=ctx.facts.baseline)
    async with ctx.workspaces.open(
        attempt=AttemptId(root="attempt"),
        item=ItemId(root="item"),
        plan=plan,
        budget=AttemptBudget(),
        within=Within(limit=LIMIT),
    ) as workspace:
        if isinstance(workspace, NotOpened):
            ctx.publish(Heard(reasons=(workspace.failed.reason,)))
    await ctx.first(ctx.stopped)


class NeverFinishes:
    """An operations executor whose request never completes on its own."""

    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.contexts: list[ExecutionContext] = []
        self._never = asyncio.Event()

    async def execute(self, request: Request, context: ExecutionContext) -> ExecutionOutcome:
        del request
        self.contexts.append(context)
        self.running.set()
        await self._never.wait()
        raise AssertionError  # pragma: no cover


def _heard(shell: CoreRuntime[FlowState]) -> tuple[FailureReason, ...]:
    heard = FlowDriver.published(shell.record.envelope.strategy, Heard)
    return () if heard is None else heard.reasons


@pytest.mark.asyncio
@given(after=st.floats(min_value=0.0, max_value=50.0, allow_nan=False))
async def test_a_request_that_never_finishes_is_cancelled_at_its_limit_and_failed(
    after: float,
) -> None:
    registry = OperationRegistry()
    driver = one_workspace.driver(Config(), registry)
    harness = Harness(registry=registry, facts=initial_state().run.facts)
    executor = NeverFinishes()
    store = FakeStateStore()
    shell = CoreRuntime(
        store,
        driver,
        new_run(driver, harness),
        config=Config(),
        bindings=CoreRuntimeBindings(
            registry=registry, executors=RequestExecutors(workspaces=executor)
        ),
    )
    shell.start("host", now_at=0, lease_duration=1e9)
    delivery = FakePublicationDelivery()

    # The request starts and is still running before its limit.
    await shell.settle(delivery, now_at=1)
    await asyncio.wait_for(executor.running.wait(), timeout=30)  # only guards against a hang
    (context,) = executor.contexts
    assert _heard(shell) == ()

    # At the limit core ends the record and the loop cancels the running executor.
    moment = context.until.at + after
    shell.submit(ClockAdvanced(now_at=moment), now_at=moment)
    await asyncio.wait_for(shell.run_until_idle(delivery, now_at=moment), timeout=30)
    (ended,) = (
        r
        for r in shell.record.envelope.core.records.records
        if isinstance(r.request, EnsureWorkspace)
    )
    assert ended.phase is RequestPhase.BLOCKED
    assert ended.failure is not None
    assert ended.failure.reason is FailureReason.TIMED_OUT
    assert _heard(shell) == (FailureReason.TIMED_OUT,)
