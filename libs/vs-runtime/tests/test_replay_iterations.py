"""Fresh epoch allowances admit additional units after historical reconstruction."""

from contextlib import suppress
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api.replay import ReplayFlow
from vs_project.api import CommitFault, FakeStateStore
from vs_proto.api import replay
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCommitError,
    ReplayCompleted,
    ReplayParked,
    ReplayResult,
    ReplayRunHost,
    ReplayRuntime,
    drive_replay,
)
from vs_sim.api.testing import Sim, swept

from .test_replay_run import Reply, call, host, launch


@given(first=st.integers(min_value=1, max_value=4), second=st.integers(min_value=0, max_value=4))
def test_raised_and_lowered_allowances_reconstruct_without_spending_again(
    first: int, second: int
) -> None:
    sim = Sim(seed=0)
    store = FakeStateStore()
    reads: list[tuple[int, int]] = []

    async def root(ctx: ReplayFlow) -> object:
        for number in range(1, 10):
            async with ctx.iteration("round", number):
                reads.append((number, ctx.allowance().iterations))
                await call(ctx, f"work/{number}")
        return "done"

    executor = FakeReplayExecutor(lambda _: ReplayResult(Reply(root="paid"), paid=True, cost_usd=1))

    async def scenario() -> None:
        def composed(allowance: int) -> ReplayRunHost:
            return replace(
                host(store, root, executor, sim),
                runtime=ReplayRuntime(store, root, replay.ReplayAllowance(iterations=allowance)),
            )

        before = composed(first)
        assert await drive_replay(before, launch("first")) == ReplayParked()
        assert len(executor.invocations) == first
        assert before.runtime.accounting.lifetime_completed == first
        reads.clear()
        after = composed(second)
        assert await drive_replay(after, launch("resume")) == ReplayParked()
        assert len(executor.invocations) == first + second
        assert reads == [(number, first) for number in range(1, first + 1)] + [
            (number, second) for number in range(first + 1, first + second + 1)
        ]
        totals = after.runtime.accounting
        assert totals.lifetime_completed == first + second
        assert totals.fresh_completed == second
        assert totals.fresh_pending == 0
        assert totals.paid_operations == first + second
        assert totals.cost_usd == first + second
        assert totals.epoch_cost_usd == second
        assert after.runtime.archive.epoch_closed

    sim.run(scenario())


@pytest.mark.parametrize("fault", list(CommitFault))
def test_crashes_at_iteration_boundaries_never_recharge_history(fault: CommitFault) -> None:
    async def root(ctx: ReplayFlow) -> object:
        async with ctx.iteration("round", 1):
            await call(ctx, "paid")
        return "done"

    sim = Sim(seed=0)
    baseline = FakeStateStore()
    executor = FakeReplayExecutor(lambda _: ReplayResult(Reply(root="paid"), paid=True, cost_usd=1))
    original = host(baseline, root, executor, sim)
    sim.run(drive_replay(original, launch("baseline")))
    count = original.runtime.archive.sequence

    async def scenario(boundary: int) -> None:
        store = FakeStateStore(fault_plan=(None,) * boundary + (fault,))
        executor = FakeReplayExecutor(
            lambda _: ReplayResult(Reply(root="paid"), paid=True, cost_usd=1)
        )
        interrupted = host(store, root, executor, sim)
        with suppress(ReplayCommitError, OSError):
            await drive_replay(interrupted, launch(f"first/{boundary}"))
        resumed = host(store, root, executor, sim)
        assert await drive_replay(resumed, launch(f"resume/{boundary}")) == ReplayCompleted("done")
        totals = resumed.runtime.accounting
        assert totals.lifetime_completed == 1
        assert totals.paid_operations == 1
        assert totals.cost_usd == 1
        assert len(executor.invocations) == 1

    for boundary in swept(count, sample=3):
        sim.run(scenario(boundary))
