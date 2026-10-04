"""The envelope owns steer reservations and acknowledgement on continuation turns."""

from hypothesis import given
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.test_lifecycle_core import _waiting_state

from vibesys.orchestration.dynamic.lifecycle import (
    CompleteIntent,
    DispatchIntent,
    EvaluationOutcome,
    IntentStage,
    LifecycleState,
    RecoveryStarted,
)
from vibesys.orchestration.dynamic.models import SteerNote
from vibesys.orchestration.dynamic.transitions import (
    EvaluationDispatchStopped,
    EvaluationSettled,
    WorkerAwaitingEvaluation,
    step,
)


@given(texts=st.lists(st.text(min_size=1, max_size=30), max_size=3))
def test_yield_acknowledges_only_its_owned_reserved_notes(texts: list[str]) -> None:
    state = _waiting_state(("a",))
    continuation = state.lifecycle.continuations["wait"]
    state.lifecycle = LifecycleState(
        intents={
            "yielded": state.lifecycle.intents["yielded"].model_copy(
                update={"stage": IntentStage.DISPATCHED},
            )
        }
    )
    assert state.agent is not None
    state.agent.steers["kept"] = [
        SteerNote(
            note_sha256="a" * 64,
            text=text,
            sent_at_s=0,
            interrupt=False,
            reserved_to="yielded",
        )
        for text in texts
    ]
    before = state.model_dump_json()
    yielded, _ = step(state, WorkerAwaitingEvaluation(continuation=continuation))
    assert state.model_dump_json() == before
    assert yielded.agent is not None
    assert all(note.delivered_to == "yielded" for note in yielded.agent.steers["kept"])


def test_resume_reservation_requires_dispatch_authority_and_acknowledgement() -> None:
    state = _waiting_state(("a",))
    assert state.agent is not None
    state.agent.steers["kept"] = [
        SteerNote(
            note_sha256="a" * 64,
            text="Check the trusted result.",
            sent_at_s=0,
            interrupt=False,
        )
    ]
    state, _ = step(
        state,
        EvaluationSettled(
            continuation_id="wait",
            scope_id="workspace",
            generation=0,
            handle="a",
            candidate_digest="a" * 64,
            evaluator_digest="b" * 64,
            workload_digest="c" * 64,
            environment_digest="d" * 64,
            outcome=EvaluationOutcome.SUCCEEDED,
            at_s=0.0,
        ),
    )
    prepared, _ = step(state, RecoveryStarted())
    assert prepared.agent is not None
    assert prepared.agent.steers["kept"][0].reserved_to is None
    stopped, _ = step(state, EvaluationDispatchStopped())
    fenced, requests = step(stopped, DispatchIntent(operation_id="wait/resume"))
    assert requests == ()
    assert fenced.agent is not None
    assert fenced.agent.steers["kept"][0].reserved_to is None
    dispatched, requests = step(state, DispatchIntent(operation_id="wait/resume"))
    assert requests
    assert dispatched.agent is not None
    assert dispatched.agent.steers["kept"][0].reserved_to == "wait/resume"
    assert dispatched.agent.steers["kept"][0].delivered_to is None
    completed, _ = step(dispatched, CompleteIntent(operation_id="wait/resume"))
    assert completed.agent is not None
    assert completed.agent.steers["kept"][0].delivered_to == "wait/resume"
