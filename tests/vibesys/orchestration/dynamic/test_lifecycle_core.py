"""Deterministic replay and atomic settlement over generated lifecycle traces."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from vibesys.orchestration.dynamic import DynamicState
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CompleteIntent,
    DispatchIntent,
    EvaluationContinuation,
    EvaluationDependency,
    EvaluationOutcome,
    IntentKind,
    IntentStage,
    LifecycleEvent,
    LifecycleIntent,
    LifecycleState,
    ObserveEvaluations,
    PrepareIntent,
    RecoveryStarted,
    ResumeAgentTurn,
)
from vibesys.orchestration.dynamic.lifecycle import (
    step as ledger_step,
)
from vibesys.orchestration.dynamic.models import (
    AgentLoopState,
    ImplementerReply,
    JournalEntry,
    JudgeReply,
    WaitingForEvaluation,
    WorkstreamPhase,
)
from vibesys.orchestration.dynamic.steers import enqueue, pending
from vibesys.orchestration.dynamic.transitions import (
    AlreadySettledError,
    EvaluationDispatchStopped,
    EvaluationSettled,
    EvaluationWaitReopened,
    InterruptedTurnReplaced,
    SettlementProposed,
    WithdrawRequested,
    WorkerAwaitingEvaluation,
    step,
)
from vs_loop_state.api import CandidateDisposition, RoundRecord


def _initial() -> DynamicState:
    data = json.loads((Path(__file__).parent / "fixtures/state_v6/completed.json").read_text())
    for hypothesis in data["search"]["hypotheses"]:
        hypothesis["rounds"] = []
        hypothesis["declared_outcome"] = None
        hypothesis["resolution"] = None
        hypothesis["candidate_retained"] = None
    state = DynamicState.model_validate_json(json.dumps(data))
    state.agent = AgentLoopState()
    state.workstreams[0].phase = WorkstreamPhase.IMPLEMENTING
    state.winner_revision = None
    return state


def _settlement(state: DynamicState, operation_id: str) -> SettlementProposed:
    item = state.workstreams[0]
    journal = tuple(
        JournalEntry(at_s=0, turn=0, kind="steer", subject=item.hypothesis_id, text=note.text)
        for note in pending(state, item.hypothesis_id)
    )
    return SettlementProposed(
        operation_id=operation_id,
        retry_limit=3,
        at_s=0,
        drop_journal=journal,
        record=RoundRecord(
            round_number=item.sequence,
            commit=item.candidate_revision,
            perf_metric=None,
            perf_unit=None,
            hypothesis_id=item.hypothesis_id,
            passed=True,
            reviewed=True,
            judge_verdict="pass",
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER.value,
            candidate_retained=True,
        ),
    )


@given(
    actions=st.lists(
        st.sampled_from(["steer", "interrupt", "withdraw", "dispatch", "settle", "restart"]),
        max_size=40,
    )
)
def test_generated_traces_keep_intents_replayable_and_settlement_atomic(actions: list[str]) -> None:
    state = _initial()
    withdrawal: str | None = None
    for sequence, action in enumerate(actions):
        before = state.model_dump_json(round_trip=True)
        if action == "steer" and withdrawal is None:
            enqueue(state, "kept", f"note-{sequence}", at_s=0, interrupt=False)
        elif (
            action == "interrupt"
            and withdrawal is None
            and state.workstreams[0].budget.refunded < 3
        ):
            intent = LifecycleIntent(
                operation_id=f"interrupt-{sequence}",
                scope_id="kept",
                generation=1,
                kind=IntentKind.INTERRUPT,
            )
            state.lifecycle, _ = ledger_step(state.lifecycle, PrepareIntent(intent=intent))
            prior = state.model_copy(deep=True)
            event = InterruptedTurnReplaced(
                scope_id="kept", revision=f"wip-{sequence}", retry_limit=3
            )
            state, _ = step(prior, event)
            assert prior.model_dump_json(round_trip=True) != state.model_dump_json(round_trip=True)
            assert step(state, event)[0] == state
            assert state.workstreams[0].candidate_revision == event.revision
        elif action == "withdraw" and withdrawal is None:
            old = state.model_copy(deep=True)
            state, effects = step(old, WithdrawRequested(scope_id="kept", kind=IntentKind.CANCEL))
            assert old.model_dump_json(round_trip=True) == before
            withdrawal = effects[0].operation_id
        elif action == "dispatch" and withdrawal is not None:
            state.lifecycle, _ = ledger_step(
                state.lifecycle, DispatchIntent(operation_id=withdrawal)
            )
        elif action == "settle" and withdrawal is not None:
            proposal = _settlement(state, withdrawal)
            old = state.model_copy(deep=True)
            state, _ = step(old, proposal)
            assert old.model_dump_json(round_trip=True) == before
            assert step(state, proposal)[0] == state
        elif action == "restart":
            state = DynamicState.model_validate_json(
                state.model_dump_json(round_trip=True), strict=True
            )
            _, replay = ledger_step(state.lifecycle, RecoveryStarted())
            assert all(intent.stage is not IntentStage.COMPLETED for intent in replay)
        if state.workstreams[0].phase is WorkstreamPhase.CANCELLED:
            assert withdrawal is not None
            assert len(state.search.rounds) == 1
            assert state.search.rounds[0].candidate_retained is False
            assert (
                state.search.rounds[0].candidate_disposition == CandidateDisposition.DISCARD.value
            )
            assert not pending(state, "kept")
            assert all(
                intent.stage is IntentStage.COMPLETED for intent in state.lifecycle.intents.values()
            )
            with pytest.raises(AlreadySettledError):
                step(state, WithdrawRequested(scope_id="kept", kind=IntentKind.PARK))


@given(kind=st.sampled_from(IntentKind), stage=st.sampled_from(IntentStage))
def test_ledger_identity_and_stage_validation_reject_corrupt_inputs(
    kind: IntentKind,
    stage: IntentStage,
) -> None:
    if kind in {IntentKind.OBSERVE, IntentKind.RESUME}:
        with pytest.raises(ValidationError, match="continuation_id"):
            LifecycleIntent(
                operation_id="operation", scope_id="scope", generation=1, kind=kind, stage=stage
            )
        return
    intent = LifecycleIntent(
        operation_id="operation",
        scope_id="scope",
        generation=1,
        kind=kind,
        stage=stage,
        invocation_id="operation" if kind is IntentKind.TURN else None,
    )
    with pytest.raises(ValidationError, match=r"lifecycle\.intents key"):
        LifecycleState(intents={"different": intent})
    if stage is IntentStage.PREPARED:
        assert PrepareIntent(intent=intent).intent == intent
    else:
        with pytest.raises(ValidationError, match=r"intent\.stage must be prepared"):
            PrepareIntent(intent=intent)
    completed, _ = ledger_step(
        LifecycleState(intents={"operation": intent}), CompleteIntent(operation_id="operation")
    )
    assert ledger_step(completed, BlockIntent(operation_id="operation"))[0] == completed


@pytest.mark.parametrize("invocation_id", [None, "different"])
def test_turn_requires_its_own_stable_invocation_identity(invocation_id: str | None) -> None:
    with pytest.raises(ValidationError, match="invocation_id must equal operation_id"):
        LifecycleIntent(
            operation_id="operation",
            scope_id="scope",
            generation=1,
            kind=IntentKind.TURN,
            invocation_id=invocation_id,
        )


@given(round_number=st.integers(min_value=2))
def test_settlement_rejects_a_different_round_generation(round_number: int) -> None:
    state, intents = step(_initial(), WithdrawRequested(scope_id="kept", kind=IntentKind.CANCEL))
    proposal = _settlement(state, intents[0].operation_id)
    assert proposal.record is not None
    invalid = proposal.model_copy(
        update={"record": replace(proposal.record, round_number=round_number)}
    )
    before = state.model_dump_json(round_trip=True)
    with pytest.raises(ValueError, match=r"record\.round_number must match intent\.generation"):
        step(state, invalid)
    assert state.model_dump_json(round_trip=True) == before


def _waiting_state(handles: tuple[str, ...] = ("a", "b")) -> DynamicState:
    state = _initial()
    turn = LifecycleIntent(
        operation_id="yielded",
        invocation_id="yielded",
        scope_id="kept",
        generation=1,
        kind=IntentKind.TURN,
        stage=IntentStage.DISPATCHED,
    )
    state.lifecycle = LifecycleState(intents={turn.operation_id: turn})
    continuation = EvaluationContinuation(
        continuation_id="wait",
        scope_id="kept",
        generation=1,
        evaluation_scope_id="workspace",
        evaluation_generation=0,
        role="implementer",
        session_key="session",
        yielded_invocation_id=turn.operation_id,
        retained_revision="retained",
        original_stage="implementing",
        dependencies=tuple(
            EvaluationDependency(
                handle=handle,
                scope_id="workspace",
                generation=0,
                candidate_revision=f"candidate-{handle}",
                candidate_digest="a" * 64,
                evaluator_digest="b" * 64,
                workload_digest="c" * 64,
                environment_digest="d" * 64,
            )
            for handle in handles
        ),
    )
    return step(state, WorkerAwaitingEvaluation(continuation=continuation))[0]


def _observation(
    handle: str, outcome: EvaluationOutcome = EvaluationOutcome.SUCCEEDED
) -> EvaluationSettled:
    return EvaluationSettled(
        continuation_id="wait",
        scope_id="workspace",
        generation=0,
        handle=handle,
        evidence_ids=(handle,),
        candidate_digest="a" * 64,
        evaluator_digest="b" * 64,
        workload_digest="c" * 64,
        environment_digest="d" * 64,
        outcome=outcome,
    )


@given(
    actions=st.lists(
        st.sampled_from(["a", "b", "unknown", "stale", "restart", "stop", "start"]), max_size=40
    )
)
def test_wait_all_generated_observations_prepare_one_resume_without_budget_changes(
    actions: list[str],
) -> None:
    state = _waiting_state()
    budget = state.workstreams[0].budget
    seen: set[str] = set()
    for action in actions:
        prior = state.model_dump_json(round_trip=True)
        if action in {"a", "b"}:
            event = _observation(action)
            seen.add(action)
        elif action == "unknown":
            event = _observation("a", EvaluationOutcome.UNKNOWN)
        elif action == "stale":
            event = _observation("a").model_copy(update={"generation": 1})
        elif action in {"stop", "start"}:
            event = EvaluationDispatchStopped(stopped=action == "stop")
        else:
            state = DynamicState.model_validate_json(prior, strict=True)
            _, requests = ledger_step(state.lifecycle, RecoveryStarted())
            if state.lifecycle.stopped:
                assert not requests
            continue
        old = state
        state, _ = step(old, event)
        assert old.model_dump_json(round_trip=True) == prior
        assert step(state, event)[0] == state
        assert state.workstreams[0].budget == budget
        assert state.workstreams[0].phase is WorkstreamPhase.IMPLEMENTING
        assert state.search.rounds == []
        resumes = [
            intent
            for intent in state.lifecycle.intents.values()
            if intent.kind is IntentKind.RESUME
        ]
        assert len(resumes) == (1 if seen == {"a", "b"} else 0)


@pytest.mark.parametrize(
    "boundary",
    ["yield", "observe_dispatch", "partial", "ready", "resume_dispatch", "unknown", "ack"],
)
def test_suspension_crash_replay_keeps_identity_at_each_durable_boundary(boundary: str) -> None:
    state = _waiting_state()
    if boundary != "yield":
        state.lifecycle, requests = ledger_step(
            state.lifecycle, DispatchIntent(operation_id="wait/observe")
        )
        assert isinstance(requests[0], ObserveEvaluations)
    if boundary not in {"yield", "observe_dispatch"}:
        state, _ = step(state, _observation("b", EvaluationOutcome.FAILED))
    if boundary not in {"yield", "observe_dispatch", "partial"}:
        state, _ = step(state, _observation("a", EvaluationOutcome.CANCELLED))
    if boundary in {"resume_dispatch", "unknown", "ack"}:
        state.lifecycle, requests = ledger_step(
            state.lifecycle, DispatchIntent(operation_id="wait/resume")
        )
        assert isinstance(requests[0], ResumeAgentTurn)
        assert requests[0].invocation_id == "wait/resume"
    if boundary == "unknown":
        state.lifecycle, _ = ledger_step(state.lifecycle, BlockIntent(operation_id="wait/resume"))
    elif boundary == "ack":
        state.lifecycle, _ = ledger_step(
            state.lifecycle, CompleteIntent(operation_id="wait/resume")
        )
    recovered = DynamicState.model_validate_json(
        state.model_dump_json(round_trip=True), strict=True
    )
    assert recovered == state
    _, requests = ledger_step(recovered.lifecycle, RecoveryStarted())
    if boundary in {"unknown", "ack"}:
        assert requests == ()
    else:
        assert len(requests) == 1
        assert requests[0].operation_id == (
            "wait/resume" if boundary in {"ready", "resume_dispatch"} else "wait/observe"
        )
    assert (
        step(recovered, _observation("a"))[0].workstreams[0].budget == state.workstreams[0].budget
    )


@pytest.mark.parametrize("kind", [IntentKind.PARK, IntentKind.CANCEL])
def test_withdraw_fences_resume_preserves_late_evidence_and_wait_budget(kind: IntentKind) -> None:
    state = _waiting_state()
    budget = state.workstreams[0].budget
    state, intents = step(state, WithdrawRequested(scope_id="kept", kind=kind))
    state, _ = step(state, _observation("a", EvaluationOutcome.CANCELLED))
    state, _ = step(state, _observation("b", EvaluationOutcome.FAILED))
    assert state.lifecycle.continuations["wait"].settled
    assert "wait/resume" not in state.lifecycle.intents
    if kind is IntentKind.PARK:
        state, _ = step(state, _settlement(state, intents[0].operation_id))
        assert state.workstreams[0].budget == budget
        with pytest.raises(ValueError, match="explicitly resolve"):
            step(state, EvaluationWaitReopened(continuation_id="wait"))
        state, _ = step(
            state, EvaluationWaitReopened(continuation_id="wait", resolved_cancelled_handles=("a",))
        )
        assert "wait/resume" in state.lifecycle.intents
    else:
        with pytest.raises(ValueError, match="cannot reopen"):
            step(
                state,
                EvaluationWaitReopened(continuation_id="wait", resolved_cancelled_handles=("a",)),
            )


@given(
    handles=st.lists(
        st.text(min_size=1).filter(lambda value: bool(value.strip()) and value == value.strip()),
        min_size=1,
        max_size=8,
        unique=True,
    )
)
def test_waiting_reply_is_strict_discriminated_and_roundtrips(handles: list[str]) -> None:
    payload = {"kind": "waiting_for_evaluation", "handles": handles}
    for reply_type in (ImplementerReply, JudgeReply):
        adapter = TypeAdapter(reply_type)
        reply = adapter.validate_json(json.dumps(payload), strict=True)
        assert isinstance(reply, WaitingForEvaluation)
        assert adapter.validate_json(adapter.dump_json(reply), strict=True) == reply
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps({**payload, "unexpected": True}))
        with pytest.raises(ValidationError):
            adapter.validate_json(json.dumps({**payload, "handles": [handles[0], handles[0]]}))


def test_schema_eight_migration_preserves_ledger_and_adds_empty_continuations() -> None:
    state = _initial()
    payload = json.loads(state.model_dump_json(round_trip=True))
    payload["schema_version"] = 8
    payload["lifecycle"].pop("continuations")
    payload["lifecycle"].pop("stopped")
    loaded = DynamicState.model_validate_json(json.dumps(payload), strict=True)
    assert loaded.schema_version == 9
    assert loaded.lifecycle.continuations == {}


@pytest.mark.parametrize(
    "stage", [IntentStage.PREPARED, IntentStage.BLOCKED, IntentStage.COMPLETED]
)
def test_first_yield_requires_confirmed_dispatched_turn(stage: IntentStage) -> None:
    waiting = _waiting_state()
    continuation = waiting.lifecycle.continuations["wait"]
    state = _initial()
    turn = waiting.lifecycle.intents["yielded"].model_copy(update={"stage": stage})
    state.lifecycle = LifecycleState(intents={"yielded": turn})
    with pytest.raises(ValueError, match="dispatched yielded turn"):
        step(state, WorkerAwaitingEvaluation(continuation=continuation))


@pytest.mark.parametrize("role", ["implementer", "judge"])
def test_resumed_turn_can_yield_again_without_another_attempt_charge(role: str) -> None:
    state = _waiting_state()
    if role == "judge":
        state.workstreams[0].phase = WorkstreamPhase.IMPLEMENTED
        continuation = state.lifecycle.continuations["wait"].model_copy(
            update={"role": role, "original_stage": "implemented"}
        )
        state.lifecycle = state.lifecycle.model_copy(
            update={"continuations": {"wait": continuation}}
        )
    budget = state.workstreams[0].budget
    state, _ = step(state, _observation("a"))
    state, _ = step(state, _observation("b"))
    state.lifecycle, _ = ledger_step(state.lifecycle, DispatchIntent(operation_id="wait/resume"))
    next_wait = state.lifecycle.continuations["wait"].model_copy(
        update={
            "continuation_id": "wait-again",
            "yielded_invocation_id": "wait/resume",
            "settlements": {},
            "evidence_ids": {},
        }
    )
    state, _ = step(state, WorkerAwaitingEvaluation(continuation=next_wait))
    assert state.workstreams[0].budget == budget
    assert state.lifecycle.intents["wait/resume"].stage is IntentStage.COMPLETED
    assert state.lifecycle.continuations["wait-again"].session_key == "session"
    assert state.lifecycle.continuations["wait-again"].original_stage == (
        "implemented" if role == "judge" else "implementing"
    )


def test_stop_dispatch_preserves_preparation_and_recovery_reconciles_acceptance() -> None:
    state = _waiting_state()
    state, _ = step(state, _observation("a"))
    state, _ = step(state, _observation("b"))
    state, _ = step(state, EvaluationDispatchStopped())
    ledger, requests = ledger_step(state.lifecycle, DispatchIntent(operation_id="wait/resume"))
    assert requests == ()
    assert ledger == state.lifecycle
    state, _ = step(state, EvaluationDispatchStopped(stopped=False))
    state.lifecycle, requests = ledger_step(
        state.lifecycle, DispatchIntent(operation_id="wait/resume")
    )
    assert isinstance(requests[0], ResumeAgentTurn)
    assert requests[0].reconcile_only is False
    _, replay = ledger_step(state.lifecycle, RecoveryStarted())
    assert isinstance(replay[0], ResumeAgentTurn)
    assert replay[0].reconcile_only is True


def test_park_after_wait_all_keeps_the_same_prepared_logical_resume() -> None:
    state = _waiting_state()
    state, _ = step(state, _observation("a"))
    state, _ = step(state, _observation("b"))
    state, intents = step(state, WithdrawRequested(scope_id="kept", kind=IntentKind.PARK))
    state, _ = step(state, _settlement(state, intents[0].operation_id))
    assert state.lifecycle.intents["wait/resume"].stage is IntentStage.PREPARED
    state, _ = step(state, EvaluationWaitReopened(continuation_id="wait"))
    _, requests = ledger_step(state.lifecycle, RecoveryStarted())
    assert len(requests) == 1
    assert requests[0].operation_id == "wait/resume"


def test_late_settlement_for_replaced_workstream_retains_evidence_without_resume() -> None:
    state = _waiting_state()
    state.workstreams[0].sequence = 2
    state, _ = step(state, _observation("a"))
    state, _ = step(state, _observation("b"))
    assert state.lifecycle.continuations["wait"].settled
    assert "wait/resume" not in state.lifecycle.intents


@pytest.mark.parametrize("event", [RecoveryStarted(), DispatchIntent(operation_id="wait/resume")])
def test_envelope_fences_prepared_resume_after_workstream_generation_advances(
    event: LifecycleEvent,
) -> None:
    state = _waiting_state()
    state, _ = step(state, _observation("a"))
    state, _ = step(state, _observation("b"))
    state.workstreams[0].sequence = 2
    state, requests = step(state, event)
    assert requests == ()
    assert state.lifecycle.intents["wait/resume"].stage is IntentStage.PREPARED
    assert state.lifecycle.continuations["wait"].settled


@pytest.mark.parametrize("missing", ["yielded", "wait/observe"])
def test_persisted_continuation_rejects_missing_authority(missing: str) -> None:
    payload = json.loads(_waiting_state().model_dump_json(round_trip=True))
    payload["lifecycle"]["intents"].pop(missing)
    with pytest.raises(ValidationError, match="continuation requires"):
        DynamicState.model_validate_json(json.dumps(payload), strict=True)


@pytest.mark.parametrize("version", [True, 8.0, "8"])
def test_migration_rejects_noninteger_schema_versions(version: object) -> None:
    payload = json.loads(_initial().model_dump_json(round_trip=True))
    payload["schema_version"] = version
    with pytest.raises(ValidationError):
        DynamicState.model_validate_json(json.dumps(payload), strict=True)


def test_resumed_yield_cannot_change_session_identity() -> None:
    state = _waiting_state()
    state, _ = step(state, _observation("a"))
    state, _ = step(state, _observation("b"))
    state, _ = step(state, DispatchIntent(operation_id="wait/resume"))
    next_wait = state.lifecycle.continuations["wait"].model_copy(
        update={
            "continuation_id": "next",
            "yielded_invocation_id": "wait/resume",
            "session_key": "other",
            "settlements": {},
            "evidence_ids": {},
        }
    )
    with pytest.raises(ValueError, match="preserve session_key"):
        step(state, WorkerAwaitingEvaluation(continuation=next_wait))
