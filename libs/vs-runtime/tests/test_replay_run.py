"""The live driver owns concurrency; only serial durable outcomes reach flow code."""

from collections.abc import Callable, Coroutine

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api.replay import AcceptedReplay, ReplayCall, ReplayFlow, ReplaySource, SemanticKey
from vs_project.api import FakeStateStore
from vs_proto.api import EpochStarted, Span, core_common, replay
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCompleted,
    ReplayLaunch,
    ReplayParked,
    ReplayResult,
    ReplayRunHost,
    ReplayRuntime,
    drive_replay,
)
from vs_sim.api.testing import Sim

Reply = core_common.Identity
Root = Callable[[ReplayFlow], Coroutine[object, object, object]]


def call(ctx: ReplayFlow, key: str, policy: bytes = b"old") -> ReplaySource[Reply]:
    return ctx.call(
        ReplayCall(
            SemanticKey(key),
            Reply(root="candidate"),
            "test.v1",
            replay.ReplayPolicy(content=policy, reply_type=Reply.desc().type_name),
            Reply,
        )
    )


def launch(request: str) -> ReplayLaunch:
    return ReplayLaunch(EpochStarted(run_id="run-a"), request, "host-a", 3)


def host(
    store: FakeStateStore, root: Root, executor: FakeReplayExecutor, sim: Sim
) -> ReplayRunHost:
    return ReplayRunHost(
        ReplayRuntime(store, root, replay.ReplayAllowance(iterations=10)), executor, sim.clock
    )


async def test_live_fanout_releases_in_completion_order_and_replays_without_dispatch(
    sim: Sim,
) -> None:
    store = FakeStateStore()
    released: list[tuple[str, float]] = []

    async def perform(span: Span) -> ReplayResult:
        await sim.clock.sleep(2 if span.name == "a" else 1)
        return ReplayResult(Reply(root=span.name), paid=True)

    async def root(ctx: ReplayFlow) -> object:
        reports = ctx.channel()

        async def child(ctx: ReplayFlow, name: str) -> None:
            reports.send((await call(ctx, name)).root)

        group = ctx.group()
        group.start(lambda ctx: child(ctx, "a"))
        group.start(lambda ctx: child(ctx, "b"))
        first = await reports
        released.append((first, ctx.clock()))
        planned = await call(ctx, "plan")
        second = await reports
        released.append((second, ctx.clock()))
        await group.all()
        return first, planned.root, second

    executor = FakeReplayExecutor(perform)
    first = await drive_replay(host(store, root, executor, sim), launch("first"))
    assert first == ReplayCompleted(("b", "plan", "a"))
    assert released == [("b", 1), ("a", 2)]
    calls = executor.invocations
    released.clear()
    again = await drive_replay(host(store, root, executor, sim), launch("again"))
    assert again == first
    assert released == [("b", 1), ("a", 2)]
    assert executor.invocations == calls


@given(delay=st.integers(min_value=4, max_value=20))
def test_long_calls_keep_the_lease_alive_and_exclude_resume_downtime(delay: int) -> None:
    sim = Sim(seed=0)
    store = FakeStateStore()

    async def perform(span: Span) -> ReplayResult:
        await sim.clock.sleep(delay)
        return ReplayResult(Reply(root=span.name), paid=True)

    async def root(ctx: ReplayFlow) -> object:
        await call(ctx, "long")
        return ctx.clock()

    async def scenario() -> None:
        executor = FakeReplayExecutor(perform)
        first = await drive_replay(host(store, root, executor, sim), launch("first"))
        assert first == ReplayCompleted(delay)
        await sim.clock.sleep(100)
        again = await drive_replay(host(store, root, executor, sim), launch("again"))
        assert again == first
        assert len(executor.invocations) == 1

    sim.run(scenario())


async def test_unused_paid_history_parks_before_dispatch_and_plain_resume_continues(
    sim: Sim,
) -> None:
    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))

    async def original(ctx: ReplayFlow) -> object:
        return (await call(ctx, "original")).root

    async def edited(ctx: ReplayFlow) -> object:
        return (await call(ctx, "edited")).root

    assert await drive_replay(host(store, original, executor, sim), launch("first")) == (
        ReplayCompleted("original")
    )
    invocations = executor.invocations
    assert (
        await drive_replay(host(store, edited, executor, sim), launch("notice")) == ReplayParked()
    )
    assert executor.invocations == invocations
    assert await drive_replay(host(store, edited, executor, sim), launch("ack")) == (
        ReplayCompleted("edited")
    )
    assert len(executor.invocations) == 2


async def test_timer_only_flow_uses_durable_clock_boundaries(sim: Sim) -> None:
    async def root(ctx: ReplayFlow) -> object:
        await ctx.timer(7)
        return ctx.clock()

    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name)))
    assert await drive_replay(host(store, root, executor, sim), launch("first")) == (
        ReplayCompleted(7)
    )
    assert executor.invocations == ()


async def test_executor_failure_releases_lease_and_never_retries_uncertain_work(
    sim: Sim,
) -> None:
    async def perform(span: Span) -> ReplayResult:
        raise ValueError(span.id)

    async def root(ctx: ReplayFlow) -> object:
        return await call(ctx, "fails")

    store = FakeStateStore()
    executor = FakeReplayExecutor(perform)
    with pytest.raises(ValueError, match="operation/"):
        await drive_replay(host(store, root, executor, sim), launch("first"))
    assert len(executor.invocations) == 1
    fence = store.acquire("other-host", sim.clock.now(), 3)
    assert fence is not None
    store.release(fence, sim.clock.now())


@pytest.mark.regression
async def test_finished_branches_still_retain_all_inflight_paid_usage(sim: Sim) -> None:
    async def root(ctx: ReplayFlow) -> object:
        _, result = await ctx.first(call(ctx, "a"), call(ctx, "b"), call(ctx, "c"))
        assert isinstance(result, Reply)
        return result.root

    async def perform(span: Span) -> ReplayResult:
        await sim.clock.sleep({"a": 1, "b": 2, "c": 3}[span.name])
        return ReplayResult(Reply(root=span.name), paid=True, cost_usd=0.5)

    store = FakeStateStore()
    executor = FakeReplayExecutor(perform)
    runtime = host(store, root, executor, sim)
    assert await drive_replay(runtime, launch("first")) == ReplayParked()
    assert len(runtime.runtime.archive.history) == 3
    assert len(runtime.runtime.archive.acknowledgements.notices) == 1
    assert all(
        isinstance(item, AcceptedReplay) and item.operation().cost_usd == 0.5
        for item in runtime.runtime.archive.history
    )
    invocations = executor.invocations
    assert await drive_replay(host(store, root, executor, sim), launch("ack")) == (
        ReplayCompleted("a")
    )
    assert executor.invocations == invocations
