"""Resource admission reserves concurrent calls and charges each observation once."""

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api.replay import ReplayCall, ReplayError, ReplayFlow, ReplaySource, SemanticKey
from vs_project.api import FakeStateStore
from vs_proto.api import Span, TokenUsage, replay
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCompleted,
    ReplayParked,
    ReplayResult,
    ReplayRuntime,
    drive_replay,
)
from vs_sim.api.testing import Sim

from .test_replay_run import Reply, host, launch


def request(
    ctx: ReplayFlow, number: int, reservation: replay.ReplayResources | None
) -> ReplaySource[Reply]:
    return ctx.call(
        ReplayCall(
            SemanticKey("paid").sample(str(number)),
            Reply(root="candidate"),
            "test.v1",
            replay.ReplayPolicy(reply_type=Reply.desc().type_name, reservation=reservation),
            Reply,
        )
    )


@pytest.mark.parametrize("dimension", ["money_usd", "input_tokens", "output_tokens"])
@given(limit=st.integers(0, 20), reservation=st.integers(1, 6), data=st.data())
def test_admission_matches_resource_arithmetic_across_fresh_epochs(
    dimension: str, limit: int, reservation: int, data: st.DataObject
) -> None:
    actual = data.draw(st.integers(0, reservation))
    sim = Sim(seed=0)
    store = FakeStateStore()
    bound = replay.ReplayResources(**{dimension: limit})
    reserve = replay.ReplayResources(**{dimension: reservation})

    async def root(ctx: ReplayFlow) -> None:
        for number in range(6):
            await request(ctx, number, reserve)

    executor = FakeReplayExecutor(
        lambda _: ReplayResult(
            Reply(root="paid"),
            paid=True,
            usage=TokenUsage(
                cost_usd=actual, input=actual, cache_read=0, cache_creation=0, output=actual
            ),
        )
    )
    admitted = 0
    while admitted < 6 and admitted * actual + reservation <= limit:
        admitted += 1

    async def scenario() -> None:
        for epoch in range(1, 3):
            composed = replace(
                host(store, root, executor, sim),
                runtime=ReplayRuntime(store, root, replay.ReplayAllowance(resources=bound)),
            )
            result = await drive_replay(composed, launch(f"epoch-{epoch}"))
            expected = min(6, admitted * epoch)
            assert len(executor.invocations) == expected
            assert result == (ReplayCompleted(None) if expected == 6 else ReplayParked())
            totals = composed.runtime.accounting.resources.reported
            assert getattr(totals, dimension) == expected * actual

    sim.run(scenario())


@pytest.mark.parametrize(
    ("limits", "completed"),
    [
        (replay.ReplayResources(money_usd=3), 2),
        (replay.ReplayResources(input_tokens=15), 2),
        (replay.ReplayResources(output_tokens=5), 1),
    ],
)
async def test_resource_bounds_apply_only_to_fresh_work(
    sim: Sim, limits: replay.ReplayResources, completed: int
) -> None:
    store = FakeStateStore()
    reservation = replay.ReplayResources(money_usd=2, input_tokens=10, output_tokens=5)

    async def root(ctx: ReplayFlow) -> None:
        for number in range(10):
            await request(ctx, number, reservation)

    executor = FakeReplayExecutor(
        lambda _: ReplayResult(
            Reply(root="paid"),
            paid=True,
            usage=TokenUsage(cost_usd=1, input=3, cache_read=0, cache_creation=0, output=1),
        )
    )
    allowance = replay.ReplayAllowance(resources=limits)
    first = replace(host(store, root, executor, sim), runtime=ReplayRuntime(store, root, allowance))
    assert await drive_replay(first, launch("first")) == ReplayParked()
    assert len(executor.invocations) == completed
    after = replace(host(store, root, executor, sim), runtime=ReplayRuntime(store, root, allowance))
    assert await drive_replay(after, launch("resume")) == ReplayParked()
    assert len(executor.invocations) == completed * 2
    totals = after.runtime.accounting
    assert totals.reported_cost_usd == completed * 2
    assert totals.epoch_reported_cost_usd == completed
    assert totals.resources.reported.input_tokens == completed * 6
    assert totals.resources.epoch_reported.input_tokens == completed * 3


async def test_unreported_paid_usage_holds_its_reservation(sim: Sim) -> None:
    store = FakeStateStore()

    async def root(ctx: ReplayFlow) -> None:
        for number in range(3):
            await request(ctx, number, replay.ReplayResources(money_usd=2, input_tokens=10))

    executor = FakeReplayExecutor(lambda _: ReplayResult(Reply(root="paid"), paid=True))
    bound = replay.ReplayAllowance(resources=replay.ReplayResources(money_usd=3, input_tokens=15))
    composed = replace(host(store, root, executor, sim), runtime=ReplayRuntime(store, root, bound))
    assert await drive_replay(composed, launch("first")) == ReplayParked()
    assert len(executor.invocations) == 1
    assert composed.runtime.accounting.unreported_cost_operations == 1
    totals = composed.runtime.accounting.resources
    assert totals.committed.money_usd == 2
    assert totals.committed.input_tokens == 10
    assert totals.reported.money_usd == 0


async def test_concurrent_reservations_wait_for_usage_before_admitting_a_sibling(sim: Sim) -> None:
    store = FakeStateStore()
    active = 0
    peak = 0

    async def perform(_: Span) -> ReplayResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await sim.clock.sleep(1)
        active -= 1
        return ReplayResult(Reply(root="paid"), paid=True, cost_usd=1)

    async def root(ctx: ReplayFlow) -> None:
        async def work(number: int) -> None:
            await request(ctx, number, replay.ReplayResources(money_usd=2))

        group = ctx.group()
        group.start(lambda _: work(1))
        group.start(lambda _: work(2))
        await group.all()

    executor = FakeReplayExecutor(perform)
    bound = replay.ReplayAllowance(resources=replay.ReplayResources(money_usd=3))
    composed = replace(host(store, root, executor, sim), runtime=ReplayRuntime(store, root, bound))
    assert await drive_replay(composed, launch("first")) == ReplayCompleted(None)
    assert peak == 1
    assert len(executor.invocations) == 2


async def test_bounded_request_requires_an_explicit_reservation(sim: Sim) -> None:
    store = FakeStateStore()

    async def root(ctx: ReplayFlow) -> None:
        await request(ctx, 1, None)

    executor = FakeReplayExecutor(lambda _: ReplayResult(Reply(root="paid"), paid=True))
    bound = replay.ReplayAllowance(resources=replay.ReplayResources(money_usd=3))
    composed = replace(host(store, root, executor, sim), runtime=ReplayRuntime(store, root, bound))
    with pytest.raises(ReplayError, match=r"policy.reservation.money_usd"):
        await drive_replay(composed, launch("first"))
    assert not executor.invocations


async def test_explicitly_free_work_can_finish_with_zero_resource_allowance(sim: Sim) -> None:
    store = FakeStateStore()
    zero = replay.ReplayResources(money_usd=0, input_tokens=0, output_tokens=0)

    async def root(ctx: ReplayFlow) -> None:
        await request(ctx, 1, zero)

    executor = FakeReplayExecutor(
        lambda _: ReplayResult(
            Reply(root="free"),
            usage=TokenUsage(cost_usd=0, input=0, cache_read=0, cache_creation=0, output=0),
        )
    )
    composed = replace(
        host(store, root, executor, sim),
        runtime=ReplayRuntime(store, root, replay.ReplayAllowance(resources=zero)),
    )
    assert await drive_replay(composed, launch("first")) == ReplayCompleted(None)
    assert len(executor.invocations) == 1
    assert composed.runtime.accounting.paid_operations == 0
    assert composed.runtime.accounting.unreported_cost_operations == 0
