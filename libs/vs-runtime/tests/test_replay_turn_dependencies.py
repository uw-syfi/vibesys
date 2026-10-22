"""Native tool obligations and original turn bounds survive every complete prefix."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf import Oneof
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
    classify_context,
)
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import Committed, FakeStateStore, StoredEnvelope
from vs_proto.api import Change, ReplayStore, SpanRecord, core_common, core_vocabulary, replay
from vs_proto.api import replay_agent as agent
from vs_proto.api import replay_evaluation as evaluation
from vs_runtime.api.replay import (
    REPLAY_STORE_SCHEMA_VERSION,
    FakeReplayExecutor,
    ReplayCommitError,
    ReplayResult,
    ReplayRuntime,
    ReplayTurnTools,
    replay_measurement_plan,
)
from vs_runtime.api.slurm_evaluation_stack import plan
from vs_sim.api.testing import ManualClock

from .test_replay_run import launch
from .test_replay_sessions import SCOPE, SESSION

if TYPE_CHECKING:
    from vs_core.api.replay import ReplaySource
    from vs_project.api import CommitOutcome, StoreFence
    from vs_proto.api import Span


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


def result(span: Span) -> ReplayResult:
    if span.replay_physical is not None:
        assert span.replay is not None
        return ReplayResult(
            agent.ReplayAgentResult(
                status=core_vocabulary.ObservationStatus.SUCCEEDED,
                turn_bound=span.replay.turn_bound,
            )
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
    tools = ReplayTurnTools(runtime, turn.id, ManualClock())
    try:
        for call in call_ids:
            assert tools.submit(call, producer.id) == ReplayToolPending(producer.id)
            assert tools.submit(call, producer.id) == ReplayToolPending(producer.id)
        assert len(runtime.archive.dependencies.owed) == len(call_ids)
        tools.acknowledge(call_ids[0])
        tools.acknowledge(call_ids[0])
        wait = tools.wait(*call_ids)
        assert tools.yielded() == wait
        archive = ReplayArchive.decode(runtime.archive.encode())
        assert archive.dependencies == ReplayTurnDependencies.from_records(
            ReplayStore.from_binary(archive.encode()).records
        )
        assert archive.dependencies.yielded(turn.id) == wait
        assert len(archive.dependencies.owed) == len(call_ids) - 1
        runtime.observe(producer.id, result(producer), now=0, at=Timestamp())
        runtime.accept(producer.id, now=0, at=Timestamp())
        for call in call_ids:
            ready = tools.lookup(call)
            assert isinstance(ready, ReplayToolReady)
            assert ready.outcome().type_url == evaluation.ReplayEvaluationResult.desc().type_name
        assert tools.lookup("unknown") == ReplayToolRefused(ReplayToolRefusal.UNKNOWN_REPLY)
    finally:
        runtime.close(now=0)


@pytest.mark.parametrize(
    "invalid",
    ["unknown", "other-turn", "no-producer", "changed-call", "changed-wait", "after-yield"],
)
def test_tool_waits_reject_unowned_producers_and_changed_dependencies(invalid: str) -> None:
    runtime, turn, producer = prepared()
    tools = ReplayTurnTools(runtime, turn.id, ManualClock())
    try:
        tools.submit("call", producer.id)
        operations = {
            "unknown": partial(tools.wait, "unknown"),
            "other-turn": partial(
                ReplayTurnTools(runtime, producer.id, ManualClock()).wait, "call"
            ),
            "no-producer": partial(tools.submit, "other", "missing"),
            "changed-call": partial(tools.submit, "call", turn.id),
            "changed-wait": partial(tools.wait, "other"),
            "after-yield": partial(tools.submit, "other", producer.id),
        }
        if invalid == "changed-wait":
            tools.submit("other", producer.id)
        if invalid in {"changed-wait", "after-yield"}:
            tools.wait("call")
        with pytest.raises(ReplayError):
            operations[invalid]()
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
        assert replacement.replay_physical.deadline == within
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
            assert span.replay_physical.deadline == 30
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
        tools = ReplayTurnTools(runtime, turn.id, ManualClock())
        tools.submit("call", producer.id)
        tools.acknowledge("call")
        tools.wait("call")
        runtime.observe(producer.id, result(producer), now=0, at=Timestamp())
        runtime.accept(producer.id, now=0, at=Timestamp())
        runtime.observe(turn.id, result(turn), now=0, at=Timestamp())
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
            for owed in fresh.archive.dependencies.owed:
                fresh.tool_fact(
                    replay.ReplayToolAcknowledgement(turn=owed.turn, reply_id=owed.reply_id),
                    now=0,
                    at=Timestamp(),
                )
            for span in fresh.archive.spans:
                if span.replay_physical is not None and not span.has_field("ended_at"):
                    tools = ReplayTurnTools(fresh, span.id, ManualClock())
                    with pytest.raises(ReplayError, match="host incarnation"):
                        tools.wait("new-call")
            assert not fresh.archive.dependencies.owed
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
