"""Turn permits and operator controls hold across every complete native prefix."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf import Oneof
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import (
    ReplayArchive,
    ReplayError,
    applied_control,
    classify_control,
    stale_control,
)
from vs_faults.api import HostCrashError
from vs_project.api import FakeStateStore, RequestState
from vs_project.api.testing import FakeControlInbox
from vs_proto.api import (
    Notice,
    NoticeCode,
    Record,
    ReplayStore,
    Severity,
    Span,
    SpanRecord,
    replay,
)
from vs_runtime.api.replay import (
    ReplayCommitError,
    ReplayRuntime,
    ReplayTurnYields,
    apply_controls,
)
from vs_sim.api.testing import ManualClock

from .test_replay_run import launch
from .test_replay_turn_dependencies import (
    ToolFaultStore,
    fact_record,
    measurement_call,
    result,
    turn_call,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_core.api.replay import ReplayFlow

    Root = Callable[[ReplayFlow], Coroutine[object, object, object]]

AT = Timestamp()
Action = Literal["pause", "pause_now", "steer", "steer_turn"]


def two_turns(first: float, second: float) -> Root:
    async def flow(ctx: ReplayFlow) -> object:
        async def a(inner: ReplayFlow) -> object:
            return await turn_call(inner, "a", within=first)

        async def b(inner: ReplayFlow) -> object:
            return await turn_call(inner, "b", within=second)

        async def measurement(inner: ReplayFlow) -> object:
            return await measurement_call(inner)

        async with ctx.group() as group:
            group.start(a)
            group.start(b)
            group.start(measurement)
        return "complete"

    return flow


async def continued(ctx: ReplayFlow) -> object:
    first = await turn_call(ctx, "first")
    assert first.turn_bound is not None
    await turn_call(ctx, "continuation", origin=first.turn_bound.origin)
    await turn_call(ctx, "fresh")
    return "complete"


def started(runtime: ReplayRuntime) -> tuple[list[Span], Span]:
    spans = runtime.prepare(now=0, at=AT)
    turns = [span for span in spans if span.replay_physical is not None]
    (producer,) = [span for span in spans if span.replay_physical is None]
    return turns, producer


def origin(span: Span) -> str:
    assert span.replay is not None
    assert span.replay.turn_bound is not None
    return span.replay.turn_bound.origin


def control(
    action: Action, *, epoch: int = 1, turn: str = "", key: str = "control"
) -> replay.ReplayControl:
    match action:
        case "pause":
            value = Oneof("pause", replay.ReplayPause())
        case "pause_now":
            value = Oneof("pause_now", replay.ReplayPauseNow())
        case _:
            steer = replay.ReplaySteer(input_id=f"input-{key}", turn=turn or None)
            value = Oneof("steer", steer)
    return replay.ReplayControl(request_id=key, epoch=epoch, action=value)


def finish(runtime: ReplayRuntime, span: Span) -> None:
    runtime.observe(span.id, result(span), now=0, at=AT)
    runtime.accept(span.id, now=0, at=AT)


@pytest.mark.regression
@example(first=10, second=50)  # A scope minimum would give turn b the deadline 10.
@given(
    first=st.integers(min_value=1, max_value=500), second=st.integers(min_value=1, max_value=500)
)
def test_each_request_carries_its_own_turns_permit(first: int, second: int) -> None:
    runtime = ReplayRuntime(FakeStateStore(), two_turns(first, second), replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=AT)
    try:
        turns, producer = started(runtime)
        tools = ReplayTurnYields(runtime, ManualClock())
        for turn in turns:
            tools.submit(turn.id, "call", producer.id)
        archive = runtime.archive
        for turn, within in zip(turns, (first, second), strict=True):
            submission = archive.dependencies.submission(tools.reply_id(turn.id, "call"))
            assert submission is not None
            permit = submission.permit
            assert permit == archive.permit(turn.id)
            assert permit is not None
            assert permit.epoch == 1
            assert permit.turn == origin(turn)
            assert permit.bound is not None
            assert permit.bound.deadline == within
    finally:
        runtime.close(now=0)


@pytest.mark.parametrize("change", ["epoch", "turn", "bound"])
def test_a_request_cannot_carry_a_stale_or_borrowed_permit(change: str) -> None:
    runtime = ReplayRuntime(FakeStateStore(), two_turns(10, 50), replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=AT)
    try:
        (turn, other), producer = started(runtime)
        archive = runtime.archive
        permit = archive.permit(turn.id)
        if change == "epoch":
            permit.epoch += 1
        elif change == "turn":
            permit = archive.permit(other.id)
            permit.bound = deepcopy(archive.permit(turn.id).bound)
        else:
            permit = archive.permit(turn.id)
            permit.bound = deepcopy(archive.permit(other.id).bound)
            assert permit.bound is not None
            permit.bound.origin = origin(turn)
        fact = replay.ReplayToolSubmission(
            turn=turn.id, call_id="call", reply_id="reply", producer=producer.id, permit=permit
        )
        reason = {"epoch": "stale_epoch", "turn": "other_turn", "bound": "other_bound"}[change]
        with pytest.raises(ReplayError, match=f"permit refused: {reason}"):
            archive.append(fact_record(runtime, fact))
    finally:
        runtime.close(now=0)


@pytest.mark.regression
@example(epoch=2, action="pause_now")
@given(
    epoch=st.integers(min_value=1, max_value=4),
    action=st.sampled_from(["pause", "pause_now", "steer", "steer_turn"]),
)
def test_a_stale_control_is_recorded_stale_and_changes_nothing(epoch: int, action: Action) -> None:
    runtime = ReplayRuntime(FakeStateStore(), continued, replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=AT)
    try:
        (child,) = runtime.prepare(now=0, at=AT)
        turn = "ended-turn" if action == "steer_turn" else ""
        disposition = runtime.control(control(action, epoch=epoch, turn=turn), now=0, at=AT)
        archive = runtime.archive
        assert archive.controls.disposition("control") == disposition
        if epoch != 1 or action == "steer_turn":
            stale = (
                replay.ReplayControlStale.EPOCH if epoch != 1 else replay.ReplayControlStale.TURN
            )
            assert stale_control(disposition) == stale
            assert not runtime.paused
            assert archive.controls.draining is None
            assert archive.controls.admits(None)
            assert archive.controls.guidance(origin(child)) == ()
            assert archive.span(child.id).replay_physical == child.replay_physical
        else:
            assert applied_control(disposition) is not None
        # The archive recomputes every disposition, so neither outcome can be forged.
        item = control(action, epoch=epoch, turn=turn, key="forged")
        true = classify_control(item, archive.control_view())
        forged = replay.ReplayControlDisposition(
            control=item,
            outcome=Oneof("applied", replay.ReplayControlApplied())
            if stale_control(true) is not None
            else Oneof("stale", replay.ReplayControlStale.EPOCH),
        )
        with pytest.raises(ReplayError, match="contradicts the committed prefix"):
            archive.append(disposition_record(archive, forged))
    finally:
        runtime.close(now=0)


def disposition_record(
    archive: ReplayArchive, disposition: replay.ReplayControlDisposition
) -> Record:
    return Record(
        seq=archive.sequence + 1,
        epoch=archive.epoch,
        clock=archive.clock,
        at=AT,
        body=Oneof(
            "notice",
            Notice(
                severity=Severity.WARNING,
                code=NoticeCode.FRAMEWORK_WARNING,
                message="Operator control disposed.",
                detail=Any(type_url=disposition.desc().type_name, value=disposition.to_binary()),
            ),
        ),
    )


def test_pause_now_records_its_withdrawals_before_any_cancellation() -> None:
    runtime = ReplayRuntime(FakeStateStore(), continued, replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=AT)
    try:
        (child,) = runtime.prepare(now=0, at=AT)
        disposition = runtime.control(control("pause_now"), now=0, at=AT)
        applied = applied_control(disposition)
        assert applied is not None
        assert applied.withdrawn == [child.id]
        records = ReplayStore.from_binary(runtime.archive.encode()).records
        disposed = next(
            record.seq
            for record in records
            if record.body is not None
            and record.body.field == "notice"
            and record.body.value.detail is not None
            and record.body.value.detail.type_url == disposition.desc().type_name
        )
        physical = runtime.archive.span(child.id).replay_physical
        assert physical is not None
        assert physical.withdrawn_sequence > disposed
        assert runtime.paused
        assert runtime.prepare(now=0, at=AT) == ()
        assert not runtime.archive.controls.admits(origin(child))
        # A repeated request returns the recorded disposition and records nothing more.
        sequence = runtime.archive.sequence
        assert runtime.control(control("pause_now"), now=0, at=AT) == disposition
        assert runtime.archive.sequence == sequence
        changed = control("pause")
        with pytest.raises(ReplayError, match="contradictory content"):
            runtime.control(changed, now=0, at=AT)
    finally:
        runtime.close(now=0)


def test_pause_drains_only_its_permits_and_a_fresh_epoch_inherits_no_drain() -> None:
    store = FakeStateStore()
    runtime = ReplayRuntime(store, continued, replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=AT)
    try:
        (first,) = runtime.prepare(now=0, at=AT)
        disposition = runtime.control(control("pause"), now=0, at=AT)
        applied = applied_control(disposition)
        assert applied is not None
        assert [permit.turn for permit in applied.draining] == [origin(first)]
        finish(runtime, first)
        (continuation,) = runtime.prepare(now=0, at=AT)
        assert origin(continuation) == origin(first)
        finish(runtime, continuation)
        assert runtime.prepare(now=0, at=AT) == ()
        assert runtime.paused
    finally:
        runtime.close(now=0)
    resumed = ReplayRuntime(store, continued, replay.ReplayAllowance())
    resumed.start(launch("resume"), now=0, at=AT)
    try:
        assert resumed.archive.controls.draining is None
        (fresh,) = resumed.prepare(now=0, at=AT)
        assert origin(fresh) == fresh.parent
    finally:
        resumed.close(now=0)


@pytest.mark.parametrize("targeted", [False, True])
def test_guidance_is_consumed_whole_by_the_next_turn_start_it_applies_to(*, targeted: bool) -> None:
    runtime = ReplayRuntime(FakeStateStore(), continued, replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=AT)
    try:
        (first,) = runtime.prepare(now=0, at=AT)
        assert first.replay_physical is not None
        assert list(first.replay_physical.guidance) == []
        turn = origin(first) if targeted else ""
        runtime.control(control("steer", turn=turn, key="one"), now=0, at=AT)
        runtime.control(control("steer", key="two"), now=0, at=AT)
        assert runtime.archive.controls.guidance(origin(first)) == ("input-one", "input-two")
        finish(runtime, first)
        (continuation,) = runtime.prepare(now=0, at=AT)
        assert continuation.replay_physical is not None
        assert list(continuation.replay_physical.guidance) == ["input-one", "input-two"]
        assert runtime.archive.controls.guidance(origin(first)) == ()
        records = ReplayStore.from_binary(runtime.archive.encode()).records
        start = deepcopy(records[-1])
        assert start.body is not None
        assert isinstance(start.body.value, SpanRecord)
        span = start.body.value.span
        assert span is not None
        assert span.replay_physical is not None
        span.replay_physical.guidance = ["input-two"]
        with pytest.raises(ReplayError, match="consumes exactly the guidance"):
            ReplayArchive.decode(ReplayStore(records=[*records[:-1], start]).to_binary())
    finally:
        runtime.close(now=0)


def inbox_case(crash: int | None, *, before: bool) -> int:
    """Post three controls, crash at durable write ``crash``, then retry in a fresh epoch."""
    store = ToolFaultStore(crash, before=before)
    inbox = FakeControlInbox()
    for action, key in (("steer", "steer"), ("pause_now", "halt"), ("pause", "late")):
        inbox.post(key, control(action, key=key).to_binary(), ended=lambda: None)
    inbox.post("garbage", b"\xff", ended=lambda: None)
    runtime = ReplayRuntime(store, continued, replay.ReplayAllowance())
    try:
        runtime.start(launch("first"), now=0, at=AT)
        runtime.prepare(now=0, at=AT)
        apply_controls(runtime, inbox, now=0, at=AT)
    except HostCrashError:
        assert crash is not None
        with pytest.raises(ReplayCommitError):
            runtime.prepare(now=0, at=AT)
    finally:
        runtime.close(now=0)
    writes = len(store.gate.calls)
    store.gate.heal()
    if store.load() is None:
        assert before
        return writes
    check_inbox(store, inbox)
    resumed = ReplayRuntime(store, continued, replay.ReplayAllowance())
    resumed.start(launch("resume"), now=0, at=AT)
    try:
        apply_controls(resumed, inbox, now=0, at=AT)
        assert not inbox.pending()
    finally:
        resumed.close(now=0)
    check_inbox(store, inbox)
    return writes


def check_inbox(store: FakeStateStore, inbox: FakeControlInbox) -> None:
    """Every marked entry has its durable disposition; every withdrawal follows pause-now."""
    stored = store.load()
    assert stored is not None
    archive = ReplayArchive.decode(stored.payload)
    for key in ("steer", "halt", "late"):
        status = inbox.status(key, ended=None)
        assert status is not None
        disposition = archive.controls.disposition(key)
        if status.state is not RequestState.PENDING:
            assert disposition is not None
            applied = status.state is RequestState.APPLIED
            assert applied == (applied_control(disposition) is not None)
    garbage = inbox.status("garbage", ended=None)
    assert garbage is not None
    assert garbage.state in {RequestState.PENDING, RequestState.REJECTED}
    halted = archive.controls.disposition("halt")
    applied = None if halted is None else applied_control(halted)
    named = set() if applied is None else set(applied.withdrawn)
    for span in archive.spans:
        physical = span.replay_physical
        if physical is not None and physical.has_field("withdrawn_sequence"):
            assert span.id in named


@pytest.mark.regression
@example(ordinal=5, before=False)
@example(ordinal=5, before=True)
@given(ordinal=st.integers(min_value=1, max_value=20), before=st.booleans())
def test_control_crash_prefixes_mark_the_inbox_only_after_the_durable_disposition(
    ordinal: int, *, before: bool
) -> None:
    writes = inbox_case(None, before=before)
    inbox_case(min(ordinal, writes), before=before)
