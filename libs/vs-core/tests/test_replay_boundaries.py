"""Boundary failures are explicit and retained launch ids cannot acknowledge new notices."""

from math import inf

import pytest
from protobuf import Message, Oneof
from protobuf.wkt.google.protobuf.struct_pb import Struct
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import (
    AcceptedClock,
    AcceptedReplay,
    ReplayAcknowledgements,
    ReplayCall,
    ReplayConsumerError,
    ReplayDeadlockError,
    ReplayError,
    ReplayFlow,
    ReplayIdentityError,
    ReplayScheduler,
    ReplaySettings,
)
from vs_proto.api import (
    Change,
    Notice,
    NoticeCode,
    Record,
    RuntimeType,
    Severity,
    Span,
    SpanRecord,
    SpanStatus,
    ValidationError,
    core_control,
    replay,
)

from .test_keyed_replay import Reply, Root, accepted, ask, finish, settings


def as_record(value: AcceptedReplay) -> Record:
    return Record(
        seq=value.sequence,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=value.clock,
        body=Oneof(
            "span",
            SpanRecord(
                change=Change.ENDED,
                span=Span(
                    id="operation",
                    runtime_type=RuntimeType.OPERATION,
                    name=value.key.operation,
                    status=SpanStatus.DONE,
                    started_at=0,
                    ended_at=value.clock,
                    replay=value.operation(),
                ),
            ),
        ),
    )


async def one(ctx: ReplayFlow) -> object:
    return await ctx.call(ask("one"))


def history() -> list[AcceptedReplay]:
    run = ReplayScheduler(one, (), settings())
    return finish(run, ["a"])


def test_acceptance_uses_record_sequence_and_clock_not_updated_span_order() -> None:
    (item,) = history()
    record = as_record(item)
    assert AcceptedReplay.from_record(record) == item
    record.seq += 1
    with pytest.raises(ReplayError, match="not identify this record"):
        AcceptedReplay.from_record(record)
    record.seq = item.sequence
    record.clear_field("clock")
    with pytest.raises(ReplayError, match=r"Record.clock"):
        AcceptedReplay.from_record(record)


def test_timer_record_uses_recorded_allowance_and_rejects_missing_clock() -> None:
    record = Record(
        seq=1,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=2,
        body=Oneof(
            "notice",
            Notice(severity=Severity.WARNING, code=NoticeCode.FRAMEWORK_WARNING, message="clock"),
        ),
        replay_clock=replay.ReplayClock(allowance=replay.ReplayAllowance(remaining=7)),
    )
    clock = AcceptedClock.from_record(record)
    assert clock.clock == 2
    assert replay.ReplayAllowance.from_binary(clock.allowance_bytes).remaining == 7
    with pytest.raises(ReplayError, match="span"):
        AcceptedReplay.from_record(record)
    record.clear_field("clock")
    with pytest.raises(ReplayError, match=r"Record.clock"):
        AcceptedClock.from_record(record)


def test_contradictory_identity_digest_is_corruption() -> None:
    (item,) = history()
    operation = item.operation()
    operation.identity_digest = b"x" * 32
    with pytest.raises(ReplayError, match="contradicts retained bytes"):
        AcceptedReplay(item.sequence, item.clock, operation.to_binary())


@pytest.mark.parametrize("edit", ["sequence", "cost", "outcome"])
def test_invalid_accepted_metadata_fails_before_flow_execution(edit: str) -> None:
    (item,) = history()
    operation = item.operation()
    if edit == "sequence":
        operation.clear_field("accepted_sequence")
    elif edit == "cost":
        operation.cost_usd = inf
    else:
        operation.outcome = None
    with pytest.raises(ValidationError):
        AcceptedReplay(item.sequence, item.clock, operation.to_binary())


def test_duplicate_acceptances_and_backwards_clocks_are_rejected() -> None:
    (item,) = history()
    with pytest.raises(ReplayError, match="unique"):
        ReplayScheduler(one, [item, item], settings())
    operation = item.operation()
    duplicate = accepted(operation, 2, "same")
    with pytest.raises(ReplayError, match="multiple accepted"):
        ReplayScheduler(one, [item, duplicate], settings())
    assert operation.key is not None
    operation.key.operation = "other"
    backwards = accepted(operation, 2, "other", clock=0)
    with pytest.raises(ReplayError, match="monotone"):
        ReplayScheduler(one, [item, backwards], settings())


def test_launch_without_notice_cannot_acknowledge_a_future_notice_on_retry() -> None:
    acks = ReplayAcknowledgements().resume("launch-1")

    async def removed(_: ReplayFlow) -> object:
        return None

    run = ReplayScheduler(removed, history(), settings(), acks)
    assert run.completed
    assert run.frontier().known_cost_usd == 0.5
    notice = run.frontier().notice()
    assert notice is not None
    recorded = acks.record_notice(notice)
    assert recorded.record_notice(notice) == recorded
    assert recorded.resume("launch-1") == recorded
    assert (
        ReplayScheduler(removed, history(), settings(), recorded.resume("launch-1"))
        .frontier()
        .unused
    )
    resumed = recorded.resume("launch-2")
    assert not ReplayScheduler(removed, history(), settings(), resumed).frontier().unused
    assert resumed.resume("launch-2") == resumed
    with pytest.raises(ReplayError, match="request id"):
        resumed.resume("")


def test_notice_cost_distinguishes_unknown_from_zero() -> None:
    (item,) = history()
    operation = item.operation()
    operation.clear_field("cost_usd")

    async def removed(_: ReplayFlow) -> object:
        return None

    run = ReplayScheduler(
        removed, [AcceptedReplay(item.sequence, item.clock, operation.to_binary())], settings()
    )
    assert run.frontier().known_cost_usd == 0
    assert run.frontier().unknown_cost_count == 1
    operation.paid = False
    free = ReplayScheduler(
        removed, [AcceptedReplay(item.sequence, item.clock, operation.to_binary())], settings()
    )
    assert free.frontier().notice() is None


def test_a_retry_of_an_old_launch_cannot_acknowledge_a_second_notice() -> None:
    async def old(ctx: ReplayFlow) -> object:
        await ctx.call(ask("A"))
        return await ctx.call(ask("B"))

    async def only_a(ctx: ReplayFlow) -> object:
        return await ctx.call(ask("A"))

    async def removed(_: ReplayFlow) -> object:
        return None

    live = ReplayScheduler(old, (), settings())
    saved = finish(live, ["a", "b"])
    first = ReplayScheduler(only_a, saved, settings()).frontier().notice()
    assert first is not None
    acks = ReplayAcknowledgements().record_notice(first).resume("launch-2")
    second_frontier = ReplayScheduler(removed, saved, settings(), acks).frontier()
    assert len(second_frontier.unused) == 1
    second = second_frontier.notice()
    assert second is not None
    acks = acks.record_notice(second)
    assert acks.resume("launch-2") == acks
    final = ReplayScheduler(removed, saved, settings(), acks.resume("launch-3"))
    assert not final.frontier().unused


def test_retained_acknowledgements_reject_unknown_notices_and_duplicate_launches() -> None:
    ack = replay.ReplayAcknowledgement(notice_id="missing", launch_request_id="launch")
    with pytest.raises(ReplayError, match="unknown notice"):
        ReplayAcknowledgements(acknowledgements=(ack.to_binary(),))
    ack.clear_field("notice_id")
    with pytest.raises(ReplayError, match="repeats"):
        ReplayAcknowledgements(acknowledgements=(ack.to_binary(), ack.to_binary()))


def test_abandoned_first_sources_do_not_authorize_new_dispatch() -> None:
    async def root(ctx: ReplayFlow) -> object:
        latch = ctx.latch()
        latch.set("already ready")
        abandoned = ctx.call(ask("same", "old"))
        index, _ = await ctx.first(latch, abandoned)
        assert index == 0
        return await ctx.call(ask("same", "new"))

    run = ReplayScheduler(root, (), settings())
    (start,) = run.dispatches()
    assert start.key is not None
    assert start.key.operation == "same"
    run.accept(accepted(start, 1, "new-answer"))
    assert run.completed
    assert run.dispatches() == ()


def test_an_operation_source_is_once_only() -> None:
    async def root(ctx: ReplayFlow) -> object:
        source = ctx.call(ask("one"))
        await source
        return await source

    run = ReplayScheduler(root, (), settings())
    (start,) = run.dispatches()
    with pytest.raises(ReplayDeadlockError, match="no producer"):
        run.accept(accepted(start, 1, "a"))
    run.close()


def test_latch_handles_each_receive_once_and_sync_reads_keep_provenance() -> None:
    async def old(ctx: ReplayFlow) -> object:
        await ctx.call(ask("A"))
        return await ctx.call(ask("B"))

    async def changed(ctx: ReplayFlow) -> object:
        latch = ctx.latch()
        other = latch.handle()
        group = ctx.group()

        async def producer(child: ReplayFlow) -> object:
            reply = await child.call(ask("A"))
            latch.set(reply)
            return None

        task = group.start(producer)
        await task
        value = latch.value
        assert value is not None
        assert value.root == "a"
        assert (await latch).root == "a"
        assert (await other).root == "a"
        return await ctx.call(ask("B"))

    live = ReplayScheduler(old, (), settings())
    saved = finish(live, ["a", "b"])
    replayed = ReplayScheduler(changed, saved, settings())
    assert replayed.result == live.result
    assert not replayed.frontier().unused


def test_policy_must_name_the_typed_reply_consumer() -> None:
    async def root(ctx: ReplayFlow) -> object:
        call = ask("one")
        call.policy.reply_type = "different.Reply"
        return await ctx.call(call)

    with pytest.raises(ReplayConsumerError, match="contradicts typed consumer"):
        ReplayScheduler(root, (), settings())


@pytest.mark.parametrize("clock", [-1, inf])
def test_invalid_initial_clock_is_rejected(clock: float) -> None:
    with pytest.raises(ReplayError, match="finite and nonnegative"):
        ReplaySettings(clock, settings().initial_allowance, settings().fresh_allowance)


@pytest.mark.parametrize("read", ["latch", "group", "all"])
def test_sync_framework_reads_include_other_completed_branches(read: str) -> None:
    def root(*, explicit: bool) -> Root:
        async def flow(ctx: ReplayFlow) -> object:
            latch = ctx.latch()
            group = ctx.group()

            async def a(child: ReplayFlow) -> object:
                reply = await child.call(ask("A"))
                latch.set(reply)
                return reply

            async def b(child: ReplayFlow) -> object:
                return await child.call(ask("B"))

            task_a = group.start(a)
            task_b = group.start(b)
            await task_b
            if explicit:
                await task_a
            elif read == "latch":
                assert latch.value is not None
            elif read == "group":
                assert group.running == 0
            else:
                await group.all()
            return await ctx.call(ask("C"))

        return flow

    live = ReplayScheduler(root(explicit=True), (), settings())
    a_start, b_start = live.dispatches()
    a_result = accepted(a_start, 1, "a")
    b_result = accepted(b_start, 2, "b")
    live.accept(a_result)
    live.accept(b_result)
    (c_start,) = live.dispatches()
    c_result = accepted(c_start, 3, "c")
    live.accept(c_result)
    replayed = ReplayScheduler(root(explicit=False), [a_result, b_result, c_result], settings())
    assert replayed.result == live.result
    assert not replayed.frontier().unused


def test_paid_answers_arriving_after_their_consumer_ends_are_disclosed() -> None:
    async def root(ctx: ReplayFlow) -> object:
        return await ctx.first(ctx.call(ask("A")), ctx.call(ask("B")))

    live = ReplayScheduler(root, (), settings())
    a_start, b_start = live.dispatches()
    a_result = accepted(a_start, 1, "a")
    b_result = accepted(b_start, 2, "b")
    live.accept(a_result)
    assert live.completed
    assert live.dispatches() == ()
    live.accept(b_result)
    assert live.frontier().unused == (b_result,)
    notice = live.frontier().notice()
    assert notice is not None
    acks = ReplayAcknowledgements().record_notice(notice).resume("resume-2")
    replayed = ReplayScheduler(root, [a_result, b_result], settings(), acks)
    assert replayed.result == live.result
    assert not replayed.frontier().unused


def test_abandoned_timers_are_not_producers_for_unrelated_waits() -> None:
    async def root(ctx: ReplayFlow) -> object:
        latch = ctx.latch()
        latch.set("ready")
        await ctx.first(latch, ctx.timer(100))
        return await ctx.channel()

    with pytest.raises(ReplayDeadlockError, match="no producer"):
        ReplayScheduler(root, (), settings())


def test_unset_latch_read_tracks_the_released_knowledge_frontier() -> None:
    def root(candidate: str) -> Root:
        async def flow(ctx: ReplayFlow) -> object:
            latch = ctx.latch()
            group = ctx.group()

            async def a(child: ReplayFlow) -> object:
                return await child.call(ask("A", candidate))

            async def b(child: ReplayFlow) -> object:
                return await child.call(ask("B"))

            group.start(a)
            task_b = group.start(b)
            await task_b
            assert latch.value is None
            return await ctx.call(ask("C"))

        return flow

    live = ReplayScheduler(root("candidate-a"), (), settings())
    a_start, b_start = live.dispatches()
    a_result = accepted(a_start, 1, "a")
    b_result = accepted(b_start, 2, "b")
    live.accept(a_result)
    live.accept(b_result)
    (c_start,) = live.dispatches()
    c_result = accepted(c_start, 3, "c")
    live.accept(c_result)
    changed = ReplayScheduler(root("candidate-changed"), [a_result, b_result, c_result], settings())
    assert {item.key.operation for item in changed.frontier().unused} == {"A", "C"}
    assert changed.dispatches() == ()
    changed.close()


def test_same_reply_name_with_a_different_retained_schema_is_not_reinterpreted() -> None:
    (item,) = history()
    operation = item.operation()
    assert operation.policy is not None
    operation.policy.reply_schema_digest = b"x" * 32
    incompatible = AcceptedReplay(item.sequence, item.clock, operation.to_binary())
    with pytest.raises(ReplayConsumerError, match="historical reply schema is incompatible"):
        ReplayScheduler(one, [incompatible], settings())


def test_strategy_cannot_forge_the_writer_reply_schema_proof() -> None:
    async def root(ctx: ReplayFlow) -> object:
        call = ask("one")
        call.policy.reply_schema_digest = b"x" * 32
        return await ctx.call(call)

    with pytest.raises(ReplayConsumerError, match="proof contradicts"):
        ReplayScheduler(root, (), settings())


@pytest.mark.parametrize("reply", [Struct, core_control.ControlInput])
def test_writer_reply_proof_traverses_maps_oneofs_and_enums(reply: type[Message]) -> None:
    async def root(ctx: ReplayFlow) -> object:
        base = ask("proof")
        base.policy.reply_type = reply.desc().type_name
        return await ctx.call(
            ReplayCall(base.key, base.identity, base.contract, base.policy, reply)
        )

    run = ReplayScheduler(root, (), settings())
    (start,) = run.dispatches()
    assert start.policy is not None
    assert len(start.policy.reply_schema_digest) == 32
    run.close()


def test_prompt_calls_cannot_bypass_typed_context_classification() -> None:
    async def root(ctx: ReplayFlow) -> object:
        base = ask("prompt")
        raw = ReplayCall(base.key, Reply(root="candidate-a"), base.contract, base.policy, Reply)
        return await ctx.call(raw)

    with pytest.raises(ReplayIdentityError, match="classified typed context"):
        ReplayScheduler(root, (), settings())
