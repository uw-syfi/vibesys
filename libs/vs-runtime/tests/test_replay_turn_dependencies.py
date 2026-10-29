"""Native tool obligations and original turn bounds survive every complete prefix."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf import Oneof
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import (
    ReplayArchive,
    ReplayCall,
    ReplayError,
    ReplayFlow,
    ReplayToolPending,
    ReplayToolReady,
    ReplayToolRefusal,
    ReplayToolRefused,
    ReplayTurnDependencies,
    SemanticKey,
    SuspensionRefusal,
    classify_context,
)
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import Committed, FakeStateStore, StoredEnvelope
from vs_proto.api import (
    Change,
    Notice,
    NoticeCode,
    Record,
    ReplayStore,
    Severity,
    SpanRecord,
    core_common,
    core_vocabulary,
    replay,
)
from vs_proto.api import replay_agent as agent
from vs_proto.api import replay_evaluation as evaluation
from vs_runtime.api.replay import (
    REPLAY_STORE_SCHEMA_VERSION,
    FakeReplayExecutor,
    ReplayCommitError,
    ReplayCompleted,
    ReplayResult,
    ReplayRuntime,
    ReplayTurnYields,
    replay_measurement_plan,
    run_replay,
)
from vs_runtime.api.slurm_evaluation_stack import plan
from vs_sim.api.testing import ManualClock

from .test_replay_run import host, launch
from .test_replay_sessions import SCOPE, SESSION

if TYPE_CHECKING:
    from protobuf import Message

    from vs_core.api.replay import ReplaySource
    from vs_project.api import CommitOutcome, StoreFence
    from vs_proto.api import Span
    from vs_sim.api.testing import Sim


def turn_call(ctx: ReplayFlow, name: str, *, origin: str = "", within: float = 30) -> ReplaySource:
    artifact = core_common.ArtifactRef(
        artifact_id=core_common.ArtifactId(root="context", kind="artifact"), digest="sha256:input"
    )
    request = agent.ReplayAgentRequest(
        scope=SCOPE,
        within_seconds=within,
        action=Oneof(
            "turn",
            agent.ReplayAgentTurn(
                session=SESSION,
                invocation=name,
                workspace=SCOPE,
                prompts=[artifact],
                artifact_dependencies=[artifact],
                output_schema=core_common.SchemaRef(name="reply", version="1"),
                max_turns=1,
                continuation_of=origin or None,
                hold_credit_seconds=3,
            ),
        ),
    )
    return ctx.call(
        ReplayCall(
            SemanticKey("agent.turn").item(name),
            classify_context(request, contract="agent.v1"),
            "agent.v1",
            replay.ReplayPolicy(reply_type=agent.ReplayAgentResult.desc().type_name),
            agent.ReplayAgentResult,
        )
    )


def measurement_call(ctx: ReplayFlow) -> ReplaySource:
    request = evaluation.ReplayEvaluationRequest(
        scope=SCOPE,
        within_seconds=100,
        action=Oneof(
            "submit", evaluation.ReplaySubmitMeasurement(plan=replay_measurement_plan(plan()))
        ),
    )
    return ctx.call(
        ReplayCall(
            SemanticKey("evaluation.submit"),
            classify_context(request, contract="evaluation.v1"),
            "evaluation.v1",
            replay.ReplayPolicy(reply_type=evaluation.ReplayEvaluationResult.desc().type_name),
            evaluation.ReplayEvaluationResult,
        )
    )


async def root(ctx: ReplayFlow) -> object:
    async def turn(flow: ReplayFlow) -> object:
        return await turn_call(flow, "first")

    async def measurement(flow: ReplayFlow) -> object:
        return await measurement_call(flow)

    async with ctx.group() as group:
        group.start(turn)
        group.start(measurement)
    return "complete"


def prepared(store: FakeStateStore | None = None) -> tuple[ReplayRuntime, Span, Span]:
    runtime = ReplayRuntime(store or FakeStateStore(), root, replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=Timestamp())
    turn, producer = runtime.prepare(now=0, at=Timestamp())
    return runtime, turn, producer


def result(span: Span, suspension: replay.ReplayTurnWait | None = None) -> ReplayResult:
    if span.replay_physical is not None:
        assert span.replay is not None
        bound = span.replay.turn_bound
        assert bound is not None
        return ReplayResult(
            agent.ReplayAgentResult(
                status=core_vocabulary.ObservationStatus.SUCCEEDED,
                turn_bound=bound,
                suspension=suspension,
            ),
            turn=replay.ReplayTurnObservation(bound=bound, succeeded=True, suspension=suspension),
        )
    return ReplayResult(
        evaluation.ReplayEvaluationResult(
            status=core_vocabulary.ObservationStatus.SUCCEEDED,
            terminal=True,
            released=True,
        )
    )


@pytest.mark.regression
@example(call_ids=["lost", "received"])
@given(
    call_ids=st.lists(
        st.text(alphabet="abc", min_size=1, max_size=8), min_size=1, max_size=8, unique=True
    )
)
def test_tool_replies_and_waits_recover_without_core_state(call_ids: list[str]) -> None:
    runtime, turn, producer = prepared()
    tools = ReplayTurnYields(runtime, ManualClock())
    try:
        for call in call_ids:
            assert tools.submit(turn.id, call, producer.id) == ReplayToolPending(producer.id)
            assert tools.submit(turn.id, call, producer.id) == ReplayToolPending(producer.id)
        assert len(runtime.archive.dependencies.owed(runtime.archive.span)) == len(call_ids)
        tools.acknowledge(turn.id, call_ids[0])
        tools.acknowledge(turn.id, call_ids[0])
        wait = tools.wait(turn.id, *call_ids)
        assert tools.yielded(turn.id) == wait
        archive = ReplayArchive.decode(runtime.archive.encode())
        assert archive.dependencies == ReplayTurnDependencies.from_records(
            ReplayStore.from_binary(archive.encode()).records
        )
        assert archive.dependencies.yielded(turn.id) == wait
        assert len(archive.dependencies.owed(archive.span)) == len(call_ids) - 1
        runtime.observe(producer.id, result(producer), now=0, at=Timestamp())
        runtime.accept(producer.id, now=0, at=Timestamp())
        for call in call_ids:
            ready = tools.lookup(turn.id, call)
            assert isinstance(ready, ReplayToolReady)
            assert ready.outcome().type_url == evaluation.ReplayEvaluationResult.desc().type_name
        assert tools.lookup(turn.id, "unknown") == ReplayToolRefused(
            ReplayToolRefusal.UNKNOWN_REPLY
        )
    finally:
        runtime.close(now=0)


@pytest.mark.parametrize(
    "invalid",
    ["other-turn", "no-producer", "changed-call", "changed-wait", "after-yield"],
)
def test_tool_waits_reject_malformed_and_changed_dependencies(invalid: str) -> None:
    runtime, turn, producer = prepared()
    tools = ReplayTurnYields(runtime, ManualClock())
    try:
        tools.submit(turn.id, "call", producer.id)
        operations = {
            "other-turn": partial(
                ReplayTurnYields(runtime, ManualClock()).wait, producer.id, "call"
            ),
            "no-producer": partial(tools.submit, turn.id, "other", "missing"),
            "changed-call": partial(tools.submit, turn.id, "call", turn.id),
            "changed-wait": partial(tools.wait, turn.id, "other"),
            "after-yield": partial(tools.submit, turn.id, "other", producer.id),
        }
        if invalid == "changed-wait":
            tools.submit(turn.id, "other", producer.id)
        if invalid in {"changed-wait", "after-yield"}:
            tools.wait(turn.id, "call")
        with pytest.raises(ReplayError):
            operations[invalid]()
    finally:
        runtime.close(now=0)


@pytest.mark.regression
@example(elapsed=31.0)
@given(elapsed=st.floats(min_value=0, max_value=100))
def test_waits_go_through_the_one_suspension_gate(elapsed: float) -> None:
    """An unowned reply or a wait past the credited bound is refused by type, never recorded."""
    runtime, turn, producer = prepared()
    clock = ManualClock()
    tools = ReplayTurnYields(runtime, clock)
    try:
        runtime.renew(now=0, duration=1000)
        tools.submit(turn.id, "call", producer.id)
        assert tools.wait(turn.id, "unknown") == SuspensionRefusal.JOB_NOT_OWNED
        assert tools.yielded(turn.id) is None
        clock.advance(elapsed)
        outcome = tools.wait(turn.id, "call")
        # The bound credits the recorded hold: stated 30 seconds plus 3 seconds of credit.
        if elapsed >= 33:
            assert outcome == SuspensionRefusal.DEADLINE_EXCEEDED
            assert tools.yielded(turn.id) is None
        else:
            assert outcome == tools.yielded(turn.id)
    finally:
        runtime.close(now=0)


def test_a_recorded_wait_the_gate_refuses_cannot_load() -> None:
    runtime, turn, _ = prepared()
    try:
        wait = replay.ReplayTurnWait(turn=turn.id, reply_ids=["unknown"])
        forged = fact_record(runtime, wait)
        with pytest.raises(ReplayError, match="suspension gate: job_not_owned"):
            runtime.archive.append(forged)
    finally:
        runtime.close(now=0)


def restore_prefix(payload: bytes) -> FakeStateStore:
    store = FakeStateStore()
    fence = store.acquire("install", 0, 1)
    assert fence is not None
    assert isinstance(
        store.commit(
            None,
            StoredEnvelope(revision=0, schema_version=REPLAY_STORE_SCHEMA_VERSION, payload=payload),
            fence,
            0,
        ),
        Committed,
    )
    assert store.release(fence, 0)
    return store


@pytest.mark.regression
@example(recovered_at=50, within=100)
@given(
    recovered_at=st.integers(min_value=1, max_value=1000),
    within=st.integers(min_value=1, max_value=1000),
)
def test_crash_between_logical_and_physical_start_keeps_the_original_bound(
    recovered_at: int, within: int
) -> None:
    async def flow(ctx: ReplayFlow) -> object:
        return await turn_call(ctx, "first", within=within)

    first = ReplayRuntime(FakeStateStore(), flow, replay.ReplayAllowance())
    first.start(launch("first"), now=0, at=Timestamp())
    (child,) = first.prepare(now=0, at=Timestamp())
    records = ReplayStore.from_binary(first.archive.encode()).records
    assert records[-1].body is not None
    assert records[-1].body.field == "span"
    assert records[-1].body.value.span == child
    first.close(now=0)
    store = restore_prefix(ReplayStore(records=records[:-1]).to_binary())
    resumed = ReplayRuntime(store, flow, replay.ReplayAllowance())
    resumed.start(launch("resume"), now=recovered_at, at=Timestamp())
    try:
        assert not resumed.recovery
        (replacement,) = resumed.prepare(now=recovered_at, at=Timestamp())
        assert replacement.replay is not None
        assert replacement.replay.turn_bound is not None
        assert replacement.replay.turn_bound.deadline == within
        assert replacement.replay_physical is not None
        assert replacement.replay_physical.deadline == within + 3
    finally:
        resumed.close(now=recovered_at)


async def test_continuation_and_correction_keep_the_original_deadline_and_credit() -> None:
    async def flow(ctx: ReplayFlow) -> object:
        first = await turn_call(ctx, "first")
        assert first.turn_bound is not None
        second = await turn_call(ctx, "continuation", origin=first.turn_bound.origin, within=100)
        assert second.turn_bound == first.turn_bound
        third = await turn_call(ctx, "correction", origin=first.turn_bound.origin, within=1000)
        assert third.turn_bound == first.turn_bound
        return "complete"

    runtime = ReplayRuntime(FakeStateStore(), flow, replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=Timestamp())
    owner = FakeReplayExecutor(result)
    try:
        for now in (0, 1, 2):
            runtime.renew(now=now, duration=100)
            (span,) = runtime.prepare(now=now, at=Timestamp())
            assert span.replay_physical is not None
            assert span.replay_physical.deadline == 33
            observed = await runtime.dispatch(span.id, owner, now=now)
            runtime.observe(span.id, observed, now=now, at=Timestamp())
            runtime.accept(span.id, now=now, at=Timestamp())
        assert runtime.completed
        archive = ReplayArchive.decode(runtime.archive.encode())
        bounds = [s.replay.turn_bound for s in archive.spans if s.replay is not None]
        assert bounds
        assert all(b == bounds[0] for b in bounds)
    finally:
        runtime.close(now=2)


@pytest.mark.parametrize("field", ["deadline", "hold_credit_seconds", "origin"])
def test_turn_bound_cannot_be_changed_in_a_complete_prefix(field: str) -> None:
    runtime, turn, _ = prepared()
    try:
        altered = deepcopy(turn)
        assert altered.replay is not None
        assert altered.replay.turn_bound is not None
        setattr(altered.replay.turn_bound, field, "other" if field == "origin" else 999)
        records = ReplayStore.from_binary(runtime.archive.encode()).records
        update = deepcopy(records[-1])
        update.seq += 1
        update.body = Oneof("span", SpanRecord(change=Change.UPDATED, span=altered))
        with pytest.raises(ReplayError, match=r"immutable|logical parent"):
            ReplayArchive.decode(ReplayStore(records=[*records, update]).to_binary())
    finally:
        runtime.close(now=0)


class ToolFaultStore(FakeStateStore):
    """CAS evidence with vs-faults on either side of each durable append."""

    def __init__(self, crash: int | None, *, before: bool) -> None:
        super().__init__()
        self.before = before
        self.gate = FaultGate(
            FaultPlan(
                seed=0,
                rules=()
                if crash is None
                else (
                    FaultRule(
                        boundary=Boundary.DURABLE_WRITE,
                        target="tools",
                        at=crash,
                        fault=HostFault.CRASH_AFTER,
                    ),
                ),
            )
        )

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        write = partial(super().commit, expected_revision, envelope, fence, now)
        if self.before:
            self.gate.around(Boundary.DURABLE_WRITE, "tools", lambda: None)
            return write()
        return self.gate.around(Boundary.DURABLE_WRITE, "tools", write)


def tool_crash_case(crash: int | None, *, before: bool) -> int:
    store = ToolFaultStore(crash, before=before)
    runtime = ReplayRuntime(store, root, replay.ReplayAllowance())
    try:
        runtime.start(launch("first"), now=0, at=Timestamp())
        turn, producer = runtime.prepare(now=0, at=Timestamp())
        tools = ReplayTurnYields(runtime, ManualClock())
        tools.submit(turn.id, "call", producer.id)
        tools.acknowledge(turn.id, "call")
        tools.wait(turn.id, "call")
        runtime.observe(producer.id, result(producer), now=0, at=Timestamp())
        runtime.accept(producer.id, now=0, at=Timestamp())
        runtime.observe(turn.id, result(turn, tools.yielded(turn.id)), now=0, at=Timestamp())
        runtime.accept(turn.id, now=0, at=Timestamp())
    except HostCrashError:
        assert crash is not None
        with pytest.raises(ReplayCommitError):
            runtime.prepare(now=0, at=Timestamp())
    finally:
        runtime.close(now=0)
    writes = len(store.gate.calls)
    store.gate.heal()
    retained = store.load()
    if retained is None:
        assert before
        return writes
    archive = ReplayArchive.decode(retained.payload)
    records = ReplayStore.from_binary(retained.payload).records
    assert archive.dependencies == ReplayTurnDependencies.from_records(records)
    assert len(archive.history) <= 2
    # A crash during this recovery/acknowledgement is itself safe to repeat.
    for epoch in range(2):
        fresh = ReplayRuntime(store, root, replay.ReplayAllowance())
        fresh.start(launch(f"resume-{epoch}"), now=0, at=Timestamp())
        try:
            for owed in fresh.archive.dependencies.owed(fresh.archive.span):
                fresh.tool_fact(
                    replay.ReplayToolAcknowledgement(turn=owed.turn, reply_id=owed.reply_id),
                    now=0,
                    at=Timestamp(),
                )
            for span in fresh.archive.spans:
                if span.replay_physical is not None and not span.has_field("ended_at"):
                    tools = ReplayTurnYields(fresh, ManualClock())
                    with pytest.raises(ReplayError, match="host incarnation"):
                        tools.wait(span.id, "new-call")
            assert not fresh.archive.dependencies.owed(fresh.archive.span)
        finally:
            fresh.close(now=0)
    return writes


@pytest.mark.regression
@example(ordinal=10, before=False)
@example(ordinal=1, before=True)
@given(ordinal=st.integers(min_value=1, max_value=30), before=st.booleans())
def test_tool_crash_prefixes_never_lose_obligations_or_authorize_old_turns(
    ordinal: int, *, before: bool
) -> None:
    writes = tool_crash_case(None, before=before)
    tool_crash_case(min(ordinal, writes), before=before)


@pytest.mark.regression
@pytest.mark.parametrize("replace", [False, True])
async def test_edited_flow_settles_a_turn_that_never_had_a_physical_child(
    sim: Sim, *, replace: bool
) -> None:
    runtime, turn, _ = prepared()
    records = ReplayStore.from_binary(runtime.archive.encode()).records
    boundary = next(
        i
        for i, r in enumerate(records)
        if r.body is not None and r.body.field == "span" and r.body.value.span == turn
    )
    runtime.close(now=0)
    store = restore_prefix(ReplayStore(records=records[:boundary]).to_binary())

    async def edited(ctx: ReplayFlow) -> object:
        if replace:
            await turn_call(ctx, "edited")
        return "edited"

    owner = FakeReplayExecutor(result)
    resumed = host(store, edited, owner, sim)
    assert await run_replay(resumed, launch("edited")) == ReplayCompleted("edited")
    assert not resumed.runtime.archive.unresolved
    assert len(owner.invocations) == int(replace)
    assert turn.replay_physical is not None
    stopped = resumed.runtime.archive.span(turn.replay_physical.logical)
    assert stopped.stop is not None
    assert stopped.stop.landed
    assert stopped.replay is not None
    assert turn.replay is not None
    assert stopped.replay.turn_bound == turn.replay.turn_bound


def submission_record(
    runtime: ReplayRuntime, turn: str, producer: str, *, prefix: str = "", unknown: bool = False
) -> Record:
    fact = replay.ReplayToolSubmission(
        turn=turn,
        producer=producer,
        call_id="call",
        reply_id="reply",
        permit=runtime.archive.permit(turn),
    )
    return fact_record(runtime, fact, prefix=prefix, unknown=unknown)


def fact_record(
    runtime: ReplayRuntime, fact: Message, *, prefix: str = "", unknown: bool = False
) -> Record:
    archive = runtime.archive
    return Record(
        seq=archive.sequence + 1,
        epoch=archive.epoch,
        clock=archive.clock,
        at=Timestamp(),
        body=Oneof(
            "notice",
            Notice(
                severity=Severity.WARNING,
                code=NoticeCode.FRAMEWORK_WARNING,
                message="tool evidence",
                detail=Any(
                    type_url=prefix + fact.desc().type_name,
                    value=fact.to_binary() + (b"\xa0\x06\x01" if unknown else b""),
                ),
            ),
        ),
    )


@pytest.mark.regression
@pytest.mark.parametrize("unknown", [False, True])
def test_tool_metadata_type_urls_cannot_hide_dependency_validation(*, unknown: bool) -> None:
    runtime, turn, producer = prepared()
    try:
        archive = runtime.archive
        record = submission_record(
            runtime, turn.id, producer.id, prefix="type.googleapis.com/", unknown=unknown
        )
        if unknown:
            with pytest.raises(ReplayError, match="unknown"):
                archive.append(record)
        else:
            archive.append(record)
            assert len(archive.dependencies.owed(archive.span)) == 1
    finally:
        runtime.close(now=0)


@pytest.mark.regression
@pytest.mark.parametrize("family", [agent.ReplayAgentRequest, evaluation.ReplayEvaluationRequest])
def test_opaque_tool_requests_reject_unknown_routing_fields(family: type[Message]) -> None:
    runtime, turn, producer = prepared()
    try:
        records = ReplayStore.from_binary(runtime.archive.encode()).records
        for record in records:
            if record.body is not None and record.body.field == "span":
                span = record.body.value.span
                if span is not None and span.replay is not None and span.replay.policy is not None:
                    request = span.replay.policy.request
                    if request is not None and request.type_url == family.desc().type_name:
                        request.value += b"\xa0\x06\x01"
        records.append(submission_record(runtime, turn.id, producer.id))
        with pytest.raises(ReplayError, match="unknown"):
            ReplayArchive.decode(ReplayStore(records=records).to_binary())
    finally:
        runtime.close(now=0)


@pytest.mark.regression
def test_a_continuation_without_its_original_bound_cannot_load() -> None:
    async def flow(ctx: ReplayFlow) -> object:
        first = await turn_call(ctx, "first")
        assert first.turn_bound is not None
        await turn_call(ctx, "continuation", origin=first.turn_bound.origin)
        return "complete"

    runtime = ReplayRuntime(FakeStateStore(), flow, replay.ReplayAllowance())
    runtime.start(launch("first"), now=0, at=Timestamp())
    try:
        (first,) = runtime.prepare(now=0, at=Timestamp())
        runtime.observe(first.id, result(first), now=0, at=Timestamp())
        runtime.accept(first.id, now=0, at=Timestamp())
        runtime.prepare(now=0, at=Timestamp())
        records = ReplayStore.from_binary(runtime.archive.encode()).records
        logical = next(
            r.body.value.span
            for r in reversed(records)
            if r.body is not None
            and r.body.field == "span"
            and r.body.value.span is not None
            and r.body.value.span.replay_physical is None
            and r.body.value.span.replay is not None
            and r.body.value.span.replay.turn_bound is not None
        )
        assert logical.replay is not None
        logical.replay.clear_field("turn_bound")
        with pytest.raises(ReplayError, match="continuation requires"):
            ReplayArchive.decode(ReplayStore(records=records).to_binary())
    finally:
        runtime.close(now=0)


@pytest.mark.regression
@pytest.mark.parametrize("change", ["omitted", "unyielded", "other-bound", "other-wait"])
def test_turn_observations_must_match_their_bound_and_durable_yield(change: str) -> None:
    runtime, turn, producer = prepared()
    tools = ReplayTurnYields(runtime, ManualClock())
    try:
        tools.submit(turn.id, "call", producer.id)
        wait = tools.wait(turn.id, "call")
        assert isinstance(wait, replay.ReplayTurnWait)
        observed = result(turn, wait)
        assert observed.turn is not None
        bad = deepcopy(observed.turn)
        if change == "omitted":
            bad = None
        elif change == "unyielded":
            bad.clear_field("suspension")
        elif change == "other-bound":
            assert bad.bound is not None
            bad.bound.hold_credit_seconds += 1
        else:
            bad.suspension = replay.ReplayTurnWait(turn=turn.id, reply_ids=["other"])
        with pytest.raises(ReplayError, match="turn observation"):
            runtime.observe(
                turn.id,
                ReplayResult(observed.outcome, turn=bad),
                now=0,
                at=Timestamp(),
            )
    finally:
        runtime.close(now=0)


def test_a_reply_owed_to_an_ended_turn_is_settled_by_its_ending() -> None:
    runtime, turn, producer = prepared()
    tools = ReplayTurnYields(runtime, ManualClock())
    try:
        tools.submit(turn.id, "call", producer.id)
        archive = runtime.archive
        assert len(archive.dependencies.owed(archive.span)) == 1
        runtime.withdraw(turn.id, now=0, at=Timestamp())
        archive = runtime.archive
        assert not archive.dependencies.owed(archive.span)
        assert tools.lookup(turn.id, "call") == ReplayToolPending(producer.id)
    finally:
        runtime.close(now=0)
