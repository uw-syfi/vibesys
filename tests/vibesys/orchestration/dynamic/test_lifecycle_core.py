"""Deterministic replay and atomic settlement over generated lifecycle traces."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.dynamic import DynamicState
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CompleteIntent,
    DispatchIntent,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    LifecycleState,
    PrepareIntent,
    RecoveryStarted,
)
from vibesys.orchestration.dynamic.lifecycle import (
    step as ledger_step,
)
from vibesys.orchestration.dynamic.models import AgentLoopState, JournalEntry, WorkstreamPhase
from vibesys.orchestration.dynamic.steers import enqueue, pending
from vibesys.orchestration.dynamic.transitions import (
    AlreadySettledError,
    InterruptedTurnReplaced,
    SettlementProposed,
    WithdrawRequested,
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
