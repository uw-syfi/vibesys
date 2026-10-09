"""Canonical interruption and checkpoint completion through the public kernel."""

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest
from .test_session_run_authority import checkpoint_event
from .test_session_turns import (
    invocation,
    reload_step,
    scope,
    turn,
    turn_observation,
    waiting_turn_state,
)


def interrupt_state() -> tuple[core.CoreState, core.InterruptRequested]:
    spec = turn()
    dispatched = reload_step(
        waiting_turn_state(spec), core.InputReservationRequested(invocation=invocation(spec))
    )
    decision = core.Withdraw(
        decision_id=core.DecisionId(root="interrupt"),
        scope=scope(),
        target=invocation(spec),
        disposition=core.Interrupt(),
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = dispatched.state.model_copy(
        update={"run": dispatched.state.run.model_copy(update={"receipts": (receipt,)})}
    )
    return state, core.InterruptRequested(
        invocation=invocation(spec), authority=core.RequestId(root="withdraw:interrupt")
    )


def test_run_interruption_waits_for_terminal_turn_then_checkpoint_and_completes_once() -> None:
    state, event = interrupt_state()
    interrupted = reload_step(state, event)
    assert len(interrupted.requests) == 1
    assert isinstance(interrupted.requests[0], core.CancelTurn)
    assert interrupted.state.sessions.interrupts[0].phase == "draining"
    assert reload_step(interrupted.state, event).requests == ()
    request = state.intents.intents[0].request
    assert isinstance(request, core.DispatchTurn)
    observed = core.TurnObserved(
        invocation=event.invocation,
        observation=turn_observation(
            request, terminal=True, accepted=True, status=core.ObservationStatus.CANCELLED
        ),
    )
    ended = reload_step(interrupted.state, observed)
    snapshot = next(row for row in ended.requests if isinstance(row, core.SnapshotAndRetainRun))
    assert ended.state.sessions.interrupts[0].phase == "draining"
    committed = reload_step(ended.state, checkpoint_event(state, snapshot))
    assert committed.state.sessions.interrupts[0].phase == "completed"
    assert committed.state.sessions.interrupts[0].checkpoint_authority == snapshot.request_id
    assert committed.state.sessions.run_charges == state.sessions.run_charges
    assert sum(isinstance(row, core.InterruptCompleted) for row in committed.events) == 1
    assert reload_step(committed.state, checkpoint_event(state, snapshot)).events == ()


@given(
    receipt=st.booleans(),
    accepted=st.booleans(),
    exact_target=st.booleans(),
    exact_scope=st.booleans(),
    digest=st.booleans(),
)
def test_interruption_requires_every_canonical_withdraw_fact(
    *, receipt: bool, accepted: bool, exact_target: bool, exact_scope: bool, digest: bool
) -> None:
    state, event = interrupt_state()
    prior = state.run.receipts[0]
    assert isinstance(prior.decision, core.Withdraw)
    decision = prior.decision.model_copy(
        update={
            "target": event.invocation
            if exact_target
            else event.invocation.model_copy(
                update={"invocation_id": core.InvocationId(root="other")}
            ),
            "scope": scope() if exact_scope else scope().model_copy(update={"generation": 1}),
        }
    )
    prior = prior.model_copy(
        update={
            "decision": decision,
            "payload_digest": value_digest(decision) if digest else "wrong",
            "feedback": core.Accepted(decision_id=decision.decision_id)
            if accepted
            else core.Rejected(
                decision_id=decision.decision_id,
                code=core.RejectionCode.OWNERSHIP,
                path=("target",),
                detail="rejected",
            ),
        }
    )
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": (prior,) if receipt else ()})}
    )
    result = reload_step(state, event)
    proven = receipt and accepted and exact_target and exact_scope and digest
    assert bool(result.state.sessions.interrupts) == proven
    assert any(isinstance(row, core.CancelTurn) for row in result.requests) == proven
    assert result.state.sessions.run_charges == state.sessions.run_charges
    assert result.state.sessions.inputs == state.sessions.inputs
