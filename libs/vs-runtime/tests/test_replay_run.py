"""The live driver owns concurrency; only serial durable outcomes reach flow code."""

from collections.abc import Callable, Coroutine
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from protobuf.wkt.google.protobuf.wrappers_pb import StringValue

from vs_core.api.replay import (
    AcceptedReplay,
    ReplayArchive,
    ReplayCall,
    ReplayFlow,
    ReplaySource,
    SemanticKey,
)
from vs_project.api import CommitFault, FakeStateStore, StoredEnvelope
from vs_proto.api import (
    Ended,
    EpochStarted,
    Outcome,
    Record,
    ReplayStore,
    RunFailure,
    RunFailureKind,
    Span,
    SpanStatus,
    core_common,
    replay,
)
from vs_runlog.api import current_epoch
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCommitError,
    ReplayCompleted,
    ReplayEnding,
    ReplayExecutor,
    ReplayLaunch,
    ReplayParked,
    ReplayRecoveryRequiredError,
    ReplayResult,
    ReplayRunHost,
    ReplayRuntime,
    drive_replay,
)
from vs_sim.api.testing import Sim, swept

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


def host(store: FakeStateStore, root: Root, executor: ReplayExecutor, sim: Sim) -> ReplayRunHost:
    return ReplayRunHost(
        ReplayRuntime(store, root, replay.ReplayAllowance(iterations=10)),
        executor,
        sim.clock,
        lambda _: ReplayEnding(
            Ended(outcome=Outcome.FINISHED, summary="The root completed.", resumable=True)
        ),
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


@pytest.mark.parametrize("dispatched", [False, True])
async def test_resume_inspects_an_interrupted_start_before_any_retry(
    sim: Sim, *, dispatched: bool
) -> None:
    async def root(ctx: ReplayFlow) -> object:
        return (await call(ctx, "work")).root

    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda _: ReplayResult(Reply(root="answer"), paid=True))
    crashed = host(store, root, executor, sim)
    crashed.runtime.start(launch("first"), now=sim.clock.now(), at=Timestamp())
    (span,) = crashed.runtime.prepare(now=sim.clock.now(), at=Timestamp())
    if dispatched:
        await crashed.runtime.dispatch(span.id, executor, now=sim.clock.now())
    crashed.runtime.close(now=sim.clock.now())
    resumed = host(store, root, executor, sim)
    assert await drive_replay(resumed, launch("resume")) == ReplayCompleted("answer")
    assert len(executor.invocations) == 1
    assert len(resumed.runtime.archive.history) == 1
    assert resumed.runtime.archive.unresolved == ()
    stopped = [item for item in resumed.runtime.archive.spans if item.status == SpanStatus.STOPPED]
    assert len(stopped) == int(not dispatched)
    assert (span.id in executor.invocations) is dispatched


async def test_absent_interrupted_start_uses_current_policy_when_retried(sim: Sim) -> None:
    async def original(ctx: ReplayFlow) -> object:
        return await call(ctx, "work", b"old")

    async def edited(ctx: ReplayFlow) -> object:
        return await call(ctx, "work", b"new")

    def perform(span: Span) -> ReplayResult:
        assert span.replay is not None
        assert span.replay.policy is not None
        assert span.replay.policy.content == b"new"
        return ReplayResult(Reply(root="answer"), paid=True)

    store = FakeStateStore()
    executor = FakeReplayExecutor(perform)
    crashed = host(store, original, executor, sim)
    crashed.runtime.start(launch("first"), now=0, at=Timestamp())
    crashed.runtime.prepare(now=0, at=Timestamp())
    crashed.runtime.close(now=0)
    resumed = host(store, edited, executor, sim)
    assert await drive_replay(resumed, launch("resume")) == ReplayCompleted(Reply(root="answer"))
    assert len(executor.invocations) == 1


async def test_failed_external_invocation_stays_uncertain_on_resume(sim: Sim) -> None:
    async def root(ctx: ReplayFlow) -> object:
        return await call(ctx, "work")

    async def perform(span: Span) -> ReplayResult:
        raise ValueError("unknown external state for " + span.id)

    store = FakeStateStore()
    executor = FakeReplayExecutor(perform)
    with pytest.raises(ValueError, match="unknown external state"):
        await drive_replay(host(store, root, executor, sim), launch("first"))
    with pytest.raises(ReplayRecoveryRequiredError, match="reconcile unfinished"):
        await drive_replay(host(store, root, executor, sim), launch("resume"))
    assert len(executor.invocations) == 1


async def test_recovered_paid_result_is_disclosed_before_an_edited_frontier(sim: Sim) -> None:
    async def original(ctx: ReplayFlow) -> object:
        return (await call(ctx, "original")).root

    async def edited(ctx: ReplayFlow) -> object:
        return (await call(ctx, "edited")).root

    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))
    crashed = host(store, original, executor, sim)
    crashed.runtime.start(launch("first"), now=0, at=Timestamp())
    (span,) = crashed.runtime.prepare(now=0, at=Timestamp())
    await crashed.runtime.dispatch(span.id, executor, now=0)
    crashed.runtime.close(now=0)
    resumed = host(store, edited, executor, sim)
    assert await drive_replay(resumed, launch("notice")) == ReplayParked()
    assert executor.invocations == (span.id,)
    assert len(resumed.runtime.archive.history) == 1
    assert await drive_replay(host(store, edited, executor, sim), launch("ack")) == (
        ReplayCompleted("edited")
    )
    assert len(executor.invocations) == 2


@pytest.mark.parametrize(
    "outcome", [Outcome.FINISHED, Outcome.FAILED, Outcome.STOPPED, Outcome.INTERRUPTED]
)
async def test_every_recorded_epoch_end_resumes_without_repeating_paid_work(
    sim: Sim, outcome: Outcome
) -> None:
    async def root(ctx: ReplayFlow) -> object:
        return (await call(ctx, "work")).root

    def ending(value: object) -> ReplayEnding:
        return ReplayEnding(
            Ended(
                outcome=outcome,
                summary="The epoch ended.",
                resumable=True,
                failure=RunFailure(kind=RunFailureKind.NO_RESULT, reason="declared result")
                if outcome == Outcome.FAILED
                else None,
            ),
            StringValue(value=str(value)),
        )

    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))
    first = replace(host(store, root, executor, sim), ending=ending)
    assert await drive_replay(first, launch("first")) == ReplayCompleted("work")
    assert first.runtime.archive.epoch_closed
    before = ReplayStore.from_binary(first.runtime.archive.encode()).records
    resumed = replace(host(store, root, executor, sim), ending=ending)
    assert await drive_replay(resumed, launch("resume")) == ReplayCompleted("work")
    after = ReplayStore.from_binary(resumed.runtime.archive.encode()).records
    assert after[: len(before)] == before
    ends = [end for _, end in epoch_endings(after)]
    assert [end.outcome for end in ends] == [outcome, outcome]
    assert all(end.resumable for end in ends)
    assert len(executor.invocations) == 1
    assert resumed.runtime.archive.epoch == 2
    assert resumed.runtime.archive.epoch_closed
    for epoch in resumed.runtime.archive.epochs:
        assert epoch.descriptors is not None
        assert StringValue.desc().file.name in {file.name for file in epoch.descriptors.file}
    for end in ends:
        assert end.detail is not None
        assert StringValue.from_binary(end.detail.value).value == "work"


async def test_resume_closes_an_interrupted_predecessor_without_claiming_external_termination(
    sim: Sim,
) -> None:
    async def root(ctx: ReplayFlow) -> object:
        return await call(ctx, "work")

    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))
    interrupted = host(store, root, executor, sim)
    interrupted.runtime.start(launch("first"), now=0, at=Timestamp())
    interrupted.runtime.prepare(now=0, at=Timestamp())
    interrupted.runtime.close(now=0)
    resumed = host(store, root, executor, sim)
    await drive_replay(resumed, launch("resume"))
    records = ReplayStore.from_binary(resumed.runtime.archive.encode()).records
    ends = epoch_endings(records)
    assert ends[0][0] == 1
    assert ends[0][1].outcome == Outcome.INTERRUPTED
    assert ends[0][1].resumable
    assert ends[1][0] == 2
    assert ends[1][1].outcome == Outcome.FINISHED


def epoch_endings(records: list[Record]) -> list[tuple[int, Ended]]:
    ends = []
    for record in records:
        match record.body:
            case Oneof("lifecycle", lifecycle):
                match lifecycle.state:
                    case Oneof("ended", ended):
                        ends.append((record.epoch, ended))
    return ends


def retained(store: FakeStateStore) -> ReplayStore:
    envelope = store.load()
    assert isinstance(envelope, StoredEnvelope)
    return ReplayStore.from_binary(envelope.payload)


@pytest.mark.parametrize("fault", list(CommitFault))
async def test_live_commit_crash_sweep_retains_exactly_one_logical_result(
    sim: Sim, fault: CommitFault
) -> None:
    async def root(ctx: ReplayFlow) -> object:
        return tuple([(await call(ctx, key)).root for key in ("a", "b", "c")])

    def executor() -> FakeReplayExecutor:
        return FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))

    baseline = FakeStateStore()
    assert await drive_replay(host(baseline, root, executor(), sim), launch("baseline")) == (
        ReplayCompleted(("a", "b", "c"))
    )
    for boundary in swept(len(retained(baseline).records), sample=3):
        store = FakeStateStore(fault_plan=(None,) * boundary + (fault,))
        owner = executor()
        with pytest.raises((ReplayCommitError, OSError)):
            await drive_replay(host(store, root, owner, sim), launch("first"))
        assert await drive_replay(host(store, root, owner, sim), launch("recovery")) == (
            ReplayCompleted(("a", "b", "c"))
        )
        archive = ReplayArchive.decode(retained(store).to_binary())
        assert len(archive.history) == len(owner.invocations) == 3
        assert len({value.sequence for value in archive.history}) == 3


@pytest.mark.parametrize("fault", list(CommitFault))
async def test_notice_and_ack_commit_crash_sweep_never_spends_before_disclosure(
    sim: Sim, fault: CommitFault
) -> None:
    async def original(ctx: ReplayFlow) -> object:
        return (await call(ctx, "original")).root

    async def edited(ctx: ReplayFlow) -> object:
        return (await call(ctx, "edited")).root

    def executor() -> FakeReplayExecutor:
        return FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))

    baseline = FakeStateStore()
    owner = executor()
    await drive_replay(host(baseline, original, owner, sim), launch("original"))
    original_length = len(retained(baseline).records)
    assert (
        await drive_replay(host(baseline, edited, owner, sim), launch("notice")) == ReplayParked()
    )
    await drive_replay(host(baseline, edited, owner, sim), launch("ack"))
    count = len(retained(baseline).records) - original_length
    for offset in swept(count, sample=3):
        boundary = original_length + offset
        store = FakeStateStore(fault_plan=(None,) * boundary + (fault,))
        owner = executor()
        await drive_replay(host(store, original, owner, sim), launch("original"))
        completed = False
        for attempt in range(4):
            try:
                result = await drive_replay(
                    host(store, edited, owner, sim), launch(f"edited-{attempt}")
                )
            except (ReplayCommitError, OSError):
                continue
            if result == ReplayCompleted("edited"):
                completed = True
                break
            assert result == ReplayParked()
            assert len(owner.invocations) == 1
        assert completed
        assert len(owner.invocations) == 2
        records = retained(store).records
        notice = next(
            record.seq
            for record in records
            if record.body and record.body.field == "notice" and record.body.value.detail
        )
        edited_start = next(
            record.seq
            for record in records
            if record.body
            and record.body.field == "span"
            and record.body.value.span is not None
            and record.body.value.span.name == "edited"
        )
        assert notice < edited_start


async def test_replay_uses_p1_epoch_derivation_and_explicit_declaration_amendments(
    sim: Sim,
) -> None:
    async def root(ctx: ReplayFlow) -> object:
        return (await call(ctx, "work")).root

    store = FakeStateStore()
    executor = FakeReplayExecutor(lambda _: ReplayResult(Reply(root="answer"), paid=True))
    for number in (1, 2):
        runtime = host(store, root, executor, sim)
        assert await drive_replay(runtime, launch(f"launch-{number}")) == ReplayCompleted("answer")
        records = retained(store).records
        assert runtime.runtime.epoch == number
        assert runtime.runtime.epoch_state == current_epoch(records)
        assert runtime.runtime.epoch_state.lifecycle is not None
        assert runtime.runtime.epoch_state.lifecycle.state is not None
        assert runtime.runtime.epoch_state.lifecycle.state.field == "ended"
        headers = [
            record for record in records if record.body and record.body.field == "epoch_started"
        ]
        amendments = [
            record
            for record in records
            if record.body and record.body.field == "declaration_amended"
        ]
        assert [record.body.value.epoch for record in headers] == list(range(1, number + 1))
        assert len(amendments) >= number
        assert all(record.epoch == record.body.value.epoch for record in amendments)
    assert len(executor.invocations) == 1
