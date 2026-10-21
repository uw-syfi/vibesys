"""Native record prefixes survive interruption at every public recorder boundary."""

from collections.abc import Callable, Coroutine
from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey
from vs_project.api import CommitFault, FakeStateStore
from vs_proto.api import EpochStarted, core_common, replay
from vs_runtime.api.replay import (
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
