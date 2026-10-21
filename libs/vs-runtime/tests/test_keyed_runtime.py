"""Native record prefixes survive interruption at every public recorder boundary."""

from collections.abc import Callable, Coroutine
from copy import deepcopy

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey
from vs_project.api import CommitFault, FakeStateStore, StoredEnvelope, StoreFence
from vs_proto.api import EpochStarted, ReplayStore, core_common, replay
from vs_runtime.api.replay import (
    REPLAY_STORE_SCHEMA_VERSION,
    FakeReplayExecutor,
    ReplayCommitError,
    ReplayLaunch,
    ReplayRecoveryRequiredError,
    ReplayResult,
    ReplayRuntime,
)

Reply = core_common.Identity
Root = Callable[[ReplayFlow], Coroutine[object, object, object]]
AT = Timestamp(seconds=1)


def program(
    *, candidate: str = "input", key: str = "implement", wording: bytes = b"old", count: int = 1
) -> Root:
    async def helper(ctx: ReplayFlow, number: int) -> Reply:
        return await ctx.call(
            ReplayCall(
                SemanticKey(key).item(str(number)),
                Reply(root=candidate),
                "implementation.v1",
                replay.ReplayPolicy(
                    role="implementer", content=wording, reply_type=Reply.desc().type_name
                ),
                Reply,
            )
        )

    async def root(ctx: ReplayFlow) -> object:
        return tuple([(await helper(ctx, number)).root for number in range(count)])

    return root


def launch(request_id: str = "launch-a") -> ReplayLaunch:
    return ReplayLaunch(EpochStarted(run_id="run-a"), request_id, "host-a", 100)


def start(
    store: FakeStateStore,
    root: Root | None = None,
    *,
    request_id: str = "launch-a",
    allowance: int = 10,
) -> ReplayRuntime:
    shell = ReplayRuntime(store, root or program(), replay.ReplayAllowance(iterations=allowance))
    shell.start(launch(request_id), now=0, at=AT)
    return shell


def finish(shell: ReplayRuntime, value: str = "answer") -> None:
    (span,) = shell.prepare(now=0, at=AT)
    shell.observe(span.id, ReplayResult(Reply(root=value), paid=True, cost_usd=0.5), now=0, at=AT)
    shell.accept(span.id, now=0, at=AT)


@given(
    count=st.integers(min_value=1, max_value=8), allowance=st.integers(min_value=0, max_value=20)
)
def test_policy_and_allowance_edits_reconstruct_without_dispatch(
    count: int, allowance: int
) -> None:
    store = FakeStateStore()
    shell = start(store, program(count=count))
    for index in range(count):
        finish(shell, str(index))
    shell.close(now=0)
    resumed = start(
        store, program(count=count, wording=b"new"), request_id="launch-b", allowance=allowance
    )
    assert resumed.completed
    assert resumed.result == tuple(map(str, range(count)))
    assert resumed.prepare(now=0, at=AT) == ()
    assert resumed.frontier.drift.roles == ("implementer",)
    assert len(resumed.archive.history) == count
    drift = resumed.archive.policy_drift
    assert drift is not None
    assert drift.results == count
    assert drift.roles == ["implementer"]
    sequence = resumed.archive.sequence
    assert resumed.prepare(now=0, at=AT) == ()
    assert resumed.archive.sequence == sequence
    resumed.close(now=0)


@pytest.mark.parametrize("change", ["key", "candidate"])
def test_identity_edit_pauses_once_and_ordinary_resume_acknowledges(change: str) -> None:
    store = FakeStateStore()
    shell = start(store)
    finish(shell)
    shell.close(now=0)
    root = program(key="renamed") if change == "key" else program(candidate="changed")
    changed = start(store, root, request_id="launch-b")
    assert len(changed.frontier.unused) == 1
    assert changed.frontier.known_cost_usd == 0.5
    assert changed.prepare(now=0, at=AT) == ()
    assert len(changed.archive.acknowledgements.notices) == 1
    assert changed.prepare(now=0, at=AT) == ()
    changed.close(now=0)
    resumed = start(store, root, request_id="launch-c")
    finish(resumed, "new-answer")
    assert resumed.result == ("new-answer",)
    assert len(resumed.archive.acknowledgements.notices) == 1
    resumed.close(now=0)


@pytest.mark.parametrize("boundary", ["epoch", "start", "observation", "outcome", "notice", "ack"])
def test_crash_prefixes_never_blindly_redispatch(boundary: str) -> None:
    store = FakeStateStore()
    shell = start(store)
    if boundary != "epoch":
        (span,) = shell.prepare(now=0, at=AT)
        if boundary != "start":
            shell.observe(span.id, ReplayResult(Reply(root="answer"), paid=True), now=0, at=AT)
            if boundary != "observation":
                shell.accept(span.id, now=0, at=AT)
    shell.close(now=0)
    if boundary in {"notice", "ack"}:
        shell = start(store, program(key="renamed"), request_id="notice-launch")
        assert shell.prepare(now=0, at=AT) == ()
        shell.close(now=0)
        if boundary == "ack":
            shell = start(store, program(key="renamed"), request_id="ack-launch")
            shell.close(now=0)
    # A new process receives only the store. No scheduler/index survives.
    resumed = start(
        store,
        program(key="renamed") if boundary in {"notice", "ack"} else program(),
        request_id="recovery-launch",
    )
    if boundary == "start":
        with pytest.raises(ReplayRecoveryRequiredError):
            resumed.prepare(now=0, at=AT)
        assert not resumed.completed
    elif boundary in {"observation", "outcome"}:
        assert resumed.result == ("answer",)
        assert len(resumed.archive.history) == 1
        assert resumed.prepare(now=0, at=AT) == ()
    else:
        finish(resumed)
        assert resumed.result == ("answer",)
    resumed.close(now=0)


@pytest.mark.parametrize("fault", list(CommitFault))
def test_uncertain_epoch_commit_never_authorizes_dispatch(fault: CommitFault) -> None:
    shell = ReplayRuntime(FakeStateStore(fault_plan=(fault,)), program(), replay.ReplayAllowance())
    with pytest.raises((ReplayCommitError, OSError)):
        shell.start(launch(), now=0, at=AT)
    with pytest.raises(ReplayCommitError):
        shell.prepare(now=0, at=AT)


def test_retried_launch_neither_acknowledges_nor_opens_another_epoch() -> None:
    store = FakeStateStore()
    shell = start(store)
    finish(shell)
    shell.close(now=0)
    before = deepcopy(store.load())
    with pytest.raises(ReplayCommitError, match="already recorded"):
        start(store)
    assert store.load() == before


async def test_dispatch_requires_confirmed_start_and_is_not_repeatable() -> None:
    shell = start(FakeStateStore())
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))
    with pytest.raises(ReplayCommitError, match="no current authorization"):
        await shell.dispatch("operation/missing", executor, now=0)
    (span,) = shell.prepare(now=0, at=AT)
    result = await shell.dispatch(span.id, executor, now=0)
    assert executor.invocations == (span.id,)
    with pytest.raises(ReplayCommitError, match="no current authorization"):
        await shell.dispatch(span.id, executor, now=0)
    shell.observe(span.id, result, now=0, at=AT, clock=3)
    shell.accept(span.id, now=0, at=AT)
    assert shell.result == ("implement",)
    assert shell.clock == 3
    shell.close(now=0)


async def test_expired_lease_prevents_dispatch_without_calling_executor() -> None:
    shell = start(FakeStateStore())
    (span,) = shell.prepare(now=0, at=AT)
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name)))
    with pytest.raises(ReplayCommitError, match="no current authorization"):
        await shell.dispatch(span.id, executor, now=100)
    assert executor.invocations == ()
    shell.close(now=100)


def test_timer_only_progress_is_durable_and_replays_its_allowance() -> None:
    async def root(ctx: ReplayFlow) -> object:
        await ctx.timer(4)
        return ctx.clock(), ctx.allowance().iterations

    store = FakeStateStore()
    shell = start(store, root, allowance=7)
    shell.advance_clock(4, now=0, at=AT)
    assert shell.result == (4, 7)
    shell.close(now=0)
    resumed = start(store, root, allowance=100, request_id="launch-b")
    assert resumed.result == (4, 7)
    assert resumed.allowance == replay.ReplayAllowance(iterations=100)
    resumed.close(now=0)


@given(count=st.integers(min_value=1, max_value=8))
def test_early_input_edit_discloses_entire_unused_paid_suffix_once(count: int) -> None:
    store = FakeStateStore()
    shell = start(store, program(count=count))
    for index in range(count):
        finish(shell, str(index))
    shell.close(now=0)
    changed = start(store, program(candidate="changed", count=count), request_id="launch-b")
    assert len(changed.frontier.unused) == count
    assert changed.frontier.known_cost_usd == count * 0.5
    assert changed.prepare(now=0, at=AT) == ()
    changed.close(now=0)
    resumed = start(store, program(candidate="changed", count=count), request_id="launch-c")
    for index in range(count):
        finish(resumed, str(index))
    assert resumed.completed
    assert len(resumed.archive.acknowledgements.notices) == 1
    resumed.close(now=0)


def test_live_fanout_dispatches_new_successors_while_sibling_is_in_flight() -> None:
    async def branch(ctx: ReplayFlow, item: str) -> Reply:
        return await ctx.call(
            ReplayCall(
                SemanticKey("implement").item(item),
                Reply(root=item),
                "implementation.v1",
                replay.ReplayPolicy(reply_type=Reply.desc().type_name),
                Reply,
            )
        )

    async def root(ctx: ReplayFlow) -> object:
        reports = ctx.channel()

        async def child(ctx: ReplayFlow, item: str) -> None:
            reports.send((await branch(ctx, item)).root)

        group = ctx.group()
        group.start(lambda ctx: child(ctx, "a"))
        group.start(lambda ctx: child(ctx, "b"))
        first = await reports
        planned = await branch(ctx, "plan-after-" + first)
        second = await reports
        await group.all()
        return first, planned.root, second

    store = FakeStateStore()
    shell = start(store, root)
    a, b = shell.prepare(now=0, at=AT)
    assert shell.prepare(now=0, at=AT) == ()
    shell.observe(b.id, ReplayResult(Reply(root="b"), paid=True), now=0, at=AT, clock=8)
    shell.accept(b.id, now=0, at=AT)
    (planned,) = shell.prepare(now=0, at=AT)
    shell.observe(planned.id, ReplayResult(Reply(root="planned"), paid=True), now=0, at=AT, clock=9)
    shell.accept(planned.id, now=0, at=AT)
    shell.observe(a.id, ReplayResult(Reply(root="a"), paid=True), now=0, at=AT, clock=11)
    shell.accept(a.id, now=0, at=AT)
    assert shell.result == ("b", "planned", "a")
    shell.close(now=0)
    resumed = start(store, root, request_id="launch-b")
    assert resumed.result == ("b", "planned", "a")
    assert resumed.prepare(now=0, at=AT) == ()
    resumed.close(now=0)


def test_every_record_prefix_including_recovery_notice_and_ack_is_recoverable() -> None:
    store = FakeStateStore()
    shell = start(store)
    (span,) = shell.prepare(now=0, at=AT)
    shell.observe(span.id, ReplayResult(Reply(root="answer"), paid=True), now=0, at=AT)
    shell.close(now=0)
    # Recovery itself contributes a header and acceptance, both of which are swept below.
    recovered = start(store, program(candidate="edited"), request_id="launch-b")
    assert recovered.prepare(now=0, at=AT) == ()
    recovered.close(now=0)
    acknowledged = start(store, program(candidate="edited"), request_id="launch-c")
    finish(acknowledged, "edited-answer")
    acknowledged.close(now=0)
    saved = store.load()
    assert isinstance(saved, StoredEnvelope)
    records = ReplayStore.from_binary(saved.payload).records
    for length in range(1, len(records) + 1):
        prefix = FakeStateStore()
        fence = prefix.acquire("seed-host", 0, 1)
        assert fence is not None
        prefix.commit(
            None,
            StoredEnvelope(
                revision=0,
                schema_version=REPLAY_STORE_SCHEMA_VERSION,
                payload=ReplayStore(records=records[:length]).to_binary(),
            ),
            fence,
            0,
        )
        assert prefix.release(fence, 0)
        replayed = start(prefix, program(candidate="edited"), request_id="recovery-prefix")
        accepted_before = tuple(item.sequence for item in replayed.archive.history)
        assert len(set(accepted_before)) == len(accepted_before)
        unresolved = replayed.archive.unresolved
        if unresolved:
            with pytest.raises(ReplayRecoveryRequiredError):
                replayed.prepare(now=0, at=AT)
        else:
            replayed.prepare(now=0, at=AT)
        accepted_after = tuple(item.sequence for item in replayed.archive.history)
        assert accepted_after == accepted_before
        replayed.close(now=0)


@pytest.mark.regression
@given(second_clock=st.integers(min_value=9, max_value=100))
@example(second_clock=11)
def test_observation_must_be_accepted_before_another_clock_or_result(second_clock: int) -> None:
    async def child(ctx: ReplayFlow, item: str) -> Reply:
        return await ctx.call(
            ReplayCall(
                SemanticKey("implement").item(item),
                Reply(root=item),
                "implementation.v1",
                replay.ReplayPolicy(reply_type=Reply.desc().type_name),
                Reply,
            )
        )

    async def root(ctx: ReplayFlow) -> object:
        group = ctx.group()
        group.start(lambda ctx: child(ctx, "a"))
        group.start(lambda ctx: child(ctx, "b"))
        first = await group
        assert isinstance(first, Reply)
        first_clock = ctx.clock()
        second = await group
        assert isinstance(second, Reply)
        return first.root, first_clock, second.root, ctx.clock()

    store = FakeStateStore()
    shell = start(store, root)
    a, b = shell.prepare(now=0, at=AT)
    shell.observe(b.id, ReplayResult(Reply(root="b"), paid=True), now=0, at=AT, clock=8)
    retained = store.load()
    with pytest.raises(ValueError, match=r"accept.*observation"):
        shell.observe(
            a.id, ReplayResult(Reply(root="a"), paid=True), now=0, at=AT, clock=second_clock
        )
    with pytest.raises(ValueError, match=r"accept.*observation"):
        shell.advance_clock(second_clock, now=0, at=AT)
    assert store.load() == retained
    shell.close(now=0)
    recovered = start(store, root, request_id="launch-b")
    assert len(recovered.archive.history) == 1
    assert recovered.clock == 8
    recovered.observe(
        a.id, ReplayResult(Reply(root="a"), paid=True), now=0, at=AT, clock=second_clock
    )
    recovered.accept(a.id, now=0, at=AT)
    assert recovered.result == ("b", 8, "a", second_clock)
    recovered.close(now=0)


async def test_renewal_keeps_long_turns_fenced_and_lost_renewal_halts_dispatch() -> None:
    store = FakeStateStore()
    shell = start(store)
    (span,) = shell.prepare(now=0, at=AT)
    shell.renew(now=90, duration=100)
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))
    result = await shell.dispatch(span.id, executor, now=120)
    shell.observe(span.id, result, now=120, at=AT)
    shell.accept(span.id, now=120, at=AT)
    assert shell.result == ("implement",)
    with pytest.raises(ReplayCommitError, match="lease was lost"):
        shell.renew(now=190, duration=100)
    with pytest.raises(ReplayCommitError, match="halted"):
        shell.prepare(now=190, at=AT)
    shell.close(now=190)


@pytest.mark.parametrize("fault", list(CommitFault))
def test_uncertain_renewal_never_authorizes_more_dispatch(fault: CommitFault) -> None:
    store = FakeStateStore(lease_fault_plan=(None, fault))
    shell = start(store)
    shell.prepare(now=0, at=AT)
    with pytest.raises(OSError, match="state-store"):
        shell.renew(now=1, duration=100)
    with pytest.raises(ReplayCommitError, match="halted"):
        shell.prepare(now=1, at=AT)
    shell.close(now=1)


@pytest.mark.regression
async def test_executor_inspection_ignores_changed_presentation_labels() -> None:
    shell = start(FakeStateStore())
    (span,) = shell.prepare(now=0, at=AT)
    fence = StoreFence(host_id="host-a", epoch=1, expires_at=100)
    executor = FakeReplayExecutor(lambda span: ReplayResult(Reply(root=span.name), paid=True))
    result = await executor.execute(span, fence)
    span.name = "Updated display name"
    assert await executor.inspect(span, fence) == result
    assert await executor.execute(span, fence) == result
    assert executor.invocations == (span.id,)
    shell.close(now=0)
