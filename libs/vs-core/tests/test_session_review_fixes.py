"""Public-API regressions for the review of the Sessions checkpoint/inputs leaf."""

from typing import Literal

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest
from .test_session_inputs import occurrence
from .test_session_run_authority import checkpoint_event, profiler_yield
from .test_session_sibling_fakes import fake_attempts, fake_evaluation
from .test_session_turns import interrupted_attempt_state, reload_step, scope, turn_observation
from .test_session_yield_completion import REDUCERS, commit_checkpoint, yielded_checkpoint


def interrupt_receipt(
    state: core.CoreState, ref: core.InvocationRef, refund: int = 0
) -> core.CoreState:
    decision = core.Withdraw(
        decision_id=core.DecisionId(root="interrupt"),
        scope=scope(),
        target=ref,
        disposition=core.Interrupt(refund=refund),
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    run = state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)})
    return state.model_copy(update={"run": run})


def completed_events(*results: core.Transition) -> list[core.InterruptCompleted]:
    return [e for r in results for e in r.events if isinstance(e, core.InterruptCompleted)]


# P1: interrupting an invocation whose checkpoint already committed must complete.
@settings(deadline=None, max_examples=15)
@given(before=st.integers(0, 2), after=st.integers(0, 2))
def test_interrupt_after_committed_checkpoint_completes_exactly_once(
    before: int, after: int
) -> None:
    state, event = profiler_yield()
    yielded = reload_step(state, event)
    request = yielded.requests[0]
    assert isinstance(request, core.SnapshotAndRetainRun)
    checkpoint = checkpoint_event(state, request)
    results = [reload_step(yielded.state, checkpoint)]
    for _ in range(before):
        results.append(reload_step(results[-1].state, checkpoint))
    ref = event.invocation
    armed = interrupt_receipt(results[-1].state, ref)
    results.append(
        reload_step(
            armed,
            core.InterruptRequested(
                invocation=ref, authority=core.RequestId(root="withdraw:interrupt")
            ),
        )
    )
    for _ in range(after):
        results.append(reload_step(results[-1].state, checkpoint))
    claims = results[-1].state.sessions.interrupts
    assert [c.phase for c in claims] == ["completed"]
    assert len(completed_events(*results)) == 1
    assert not any(isinstance(r, core.InvocationCheckpointRequested) for r in results[-1].requests)


# Attempt-owned interruption with a bounded refund.
def refund_state(
    charged: int, refund_limit: int
) -> tuple[core.CoreState, core.InvocationCheckpointAvailable]:
    state, ref, _succ, owner_scope = interrupted_attempt_state("draining")
    owner = state.attempts.attempts[0]
    admission = owner.admission_id
    inv = state.sessions.invocations[0]
    assert inv.observation is not None
    dispatch = core.DispatchTurn(
        request_id=inv.observation.request_id,
        scope=owner_scope,
        admission_id=admission,
        deadline_at=100.0,
        turn=inv.turn,
    )
    snap_id = core.RequestId(root="wip-checkpoint")
    snap = core.SnapshotAndRetain(
        request_id=snap_id,
        scope=owner_scope,
        admission_id=admission,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
        retention="wip",
        invocation=ref,
    )

    def intent(
        req: core.DispatchTurn | core.SnapshotAndRetain,
        lifecycle: core.LifecycleClass,
        obs: core.Observation | None = None,
    ) -> core.Intent:
        assert req.request_id is not None
        return core.Intent(
            request_id=req.request_id,
            request=req,
            payload_digest=value_digest(req),
            lifecycle=lifecycle,
            phase=core.IntentPhase.COMPLETED,
            reconcile_deadline_at=100.0,
            observation=obs,
        )

    intents = (
        intent(dispatch, core.LifecycleClass.SESSION_TURN, inv.observation),
        intent(snap, core.LifecycleClass.IDEMPOTENT_WRITE),
    )
    charges = tuple(
        c.model_copy(update={"charged": charged}) if c.kind == core.ChargeKind.ATTEMPT else c
        for c in owner.charges
    )
    cp = core.AttemptCheckpoint(
        invocation=ref,
        request_id=snap_id,
        revision=state.run.facts.baseline,
        retention="wip",
    )
    owner = owner.model_copy(
        update={
            "checkpoints": (cp,),
            "charges": charges,
            "budget": owner.budget.model_copy(update={"refund_limit": refund_limit}),
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": core.IntentsState(intents=intents),
            "run": state.run.model_copy(
                update={"limits": core.Limits(max_turns=10, max_refunds=5)}
            ),
        }
    )
    event = core.InvocationCheckpointAvailable(
        invocation=ref, request_id=snap_id, revision=cp.revision, retention="wip"
    )
    return state, event


def counting_reducers() -> tuple[core.CoreReducers, list[core.AttemptChargeRefundRequested]]:
    seen: list[core.AttemptChargeRefundRequested] = []

    def attempts(
        state: core.AttemptsState, context: core.AttemptsContext, event: core.AttemptsEvent
    ) -> core.AreaChange[core.AttemptsState]:
        if isinstance(event, core.AttemptChargeRefundRequested):
            seen.append(event)
            return core.AreaChange(state=state)  # in flight: not yet applied
        return fake_attempts(state, context, event)

    return core.CoreReducers(attempts=attempts, evaluation=fake_evaluation), seen


# P2: the refund request is idempotent per claim, however often the event repeats.
@settings(deadline=None, max_examples=15)
@given(repeats=st.integers(1, 5), charged=st.integers(1, 4))
def test_repeated_checkpoint_event_emits_one_refund_request(repeats: int, charged: int) -> None:
    state, event = refund_state(charged, refund_limit=5)
    reducers, seen = counting_reducers()
    for _ in range(repeats):
        state = core.step(state, event, reducers=reducers).state
    assert len(seen) == 1
    assert state.sessions.interrupts[0].phase == "checkpointed"


# P2: a refund the budget can no longer cover blocks the claim with no request.
@given(repeats=st.integers(1, 3))
def test_refund_guard_failure_blocks_the_claim(repeats: int) -> None:
    state, event = refund_state(charged=3, refund_limit=0)
    reducers, seen = counting_reducers()
    for _ in range(repeats):
        result = core.step(state, event, reducers=reducers)
        state = result.state
        assert not result.requests
    assert seen == []
    assert [c.phase for c in state.sessions.interrupts] == ["blocked"]


# P2: inputs released after a closed owner are disposed with the owner's reason.
def release_after_closure(disposition: Literal["park", "cancel", "settle"]) -> core.Transition:
    state, ref, _succ, owner_scope = interrupted_attempt_state("pending")
    owner = state.attempts.attempts[0]
    adm = owner.admission_id
    assert adm is not None
    item = occurrence(0, 0, core.ScopeInputTarget(scope=owner_scope))
    inv = state.sessions.invocations[0]
    req = core.DispatchTurn(
        request_id=core.RequestId(root="old-dispatch"),
        scope=owner_scope,
        admission_id=adm,
        deadline_at=100.0,
        turn=inv.turn,
        inputs=(item,),
    )
    obs = turn_observation(
        req, terminal=True, accepted=False, status=core.ObservationStatus.CANCELLED
    ).model_copy(update={"admission_id": adm})
    inv = inv.model_copy(update={"observation": obs, "input_ids": (item.input_id,)})
    assert req.request_id is not None
    intent = core.Intent(
        request_id=req.request_id,
        request=req,
        payload_digest=value_digest(req),
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.COMPLETED,
        reconcile_deadline_at=100.0,
        observation=obs,
    )
    closure = core.AttemptClosure(
        disposition=disposition,
        requested_at=1.0,
        authority=core.RequestId(root="retire"),
        admission_id=adm,
    )
    owner = owner.model_copy(update={"closure": closure, "phase": core.AttemptPhase.CLOSING})
    seeded = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": core.IntentsState(intents=(intent,)),
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (inv,),
                    "interrupts": (),
                    "inputs": (core.InputRecord(input=item, reserved_to=ref),),
                }
            ),
        }
    )
    return core.step(seeded, core.InputReservationReleased(invocation=ref, observation=obs))


@given(disposition=st.sampled_from(["park", "cancel", "settle"]))
def test_release_after_owner_closure_drops_with_the_owner_reason(
    disposition: Literal["park", "cancel", "settle"],
) -> None:
    result = release_after_closure(disposition)
    record = result.state.sessions.inputs[0]
    if disposition == "park":
        assert record.receipt is None
        assert record.reserved_to is None
        assert not result.events
        return
    expected = (
        core.InputDropReason.OWNER_CANCELLED
        if disposition == "cancel"
        else core.InputDropReason.OWNER_TERMINAL
    )
    assert isinstance(record.receipt, core.InputDropped)
    assert record.receipt.reason == expected
    assert list(result.events) == [record.receipt]


# P2: a retained snapshot must name the invocation it retains.
def test_snapshot_request_of_another_invocation_never_completes_the_yield() -> None:
    state, event, request = yielded_checkpoint()
    other = core.InvocationRef(
        session_id=event.invocation.session_id,
        invocation_id=core.InvocationId(root="some-other-invocation"),
        generation=event.invocation.generation,
    )
    for bound in (None, other):
        forged = request.model_copy(
            update={
                "request_id": core.RequestId(root=f"stale-{bound is None}"),
                "invocation": bound,
            }
        )
        intent = next(i for i in state.intents.intents if i.request_id == request.request_id)
        forged_intent = intent.model_copy(
            update={
                "request_id": forged.request_id,
                "request": forged,
                "payload_digest": value_digest(forged),
            }
        )
        seeded = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={"intents": (*state.intents.intents, forged_intent)}
                )
            }
        )
        ev = event.model_copy(update={"request_id": forged.request_id})
        seeded = commit_checkpoint(seeded, ev)
        result = core.step(seeded, ev, reducers=REDUCERS)
        assert result.state.run.receipts[0].completion is None
        assert result.state.evaluation.continuations == ()


# P3: a checkpoint committed while the run closes is stored but publishes nothing,
# because the Continuations leaf accepts waits only under current active ownership.
def test_checkpoint_committed_while_closing_is_stored_and_not_published() -> None:
    state, event = profiler_yield()
    yielded = reload_step(state, event)
    closing = yielded.state.model_copy(
        update={"run": yielded.state.run.model_copy(update={"status": core.RunStatus.CLOSING})}
    )
    request = yielded.requests[0]
    assert isinstance(request, core.SnapshotAndRetainRun)
    committed = core.step(closing, checkpoint_event(state, request))
    assert len(committed.state.sessions.run_checkpoints) == 1
    assert committed.state.evaluation.continuations == ()
    assert committed.state.sessions.invocations[0].pending_suspension is not None
