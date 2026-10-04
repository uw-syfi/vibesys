"""Run-owned profiler checkpoint, resume and drain public traces."""

import hashlib
import json

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_continuations import fixture, job_change
from .test_session_turns import (
    invocation,
    reload_step,
    scope,
    turn,
    turn_observation,
    waiting_turn_state,
)


def profiler_yield() -> tuple[core.CoreState, core.TurnObserved]:
    spec = turn()
    dispatched = reload_step(
        waiting_turn_state(spec), core.InputReservationRequested(invocation=invocation(spec))
    )
    request = dispatched.requests[0]
    assert isinstance(request, core.DispatchTurn)
    decision_id = core.DecisionId(root="profiler-turn")
    request = request.model_copy(update={"decision_id": decision_id})
    observation = turn_observation(
        request, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    )
    decision = core.RequestTurn(decision_id=decision_id, scope=scope(), turn=spec)
    receipt = core.DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest=hashlib.sha256(
            json.dumps(
                decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        feedback=core.Accepted(decision_id=decision_id),
        request_ids=(request.request_id,),
    )
    intents = tuple(
        row.model_copy(
            update={
                "request": request,
                "payload_digest": hashlib.sha256(
                    json.dumps(
                        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                "phase": core.IntentPhase.COMPLETED,
                "observation": observation,
            }
        )
        if row.request_id == request.request_id
        else row
        for row in dispatched.state.intents.intents
    )
    state = dispatched.state.model_copy(
        update={
            "run": dispatched.state.run.model_copy(
                update={
                    "receipts": (receipt,),
                    "limits": dispatched.state.run.limits.model_copy(update={"max_turns": 10}),
                }
            ),
            "intents": dispatched.state.intents.model_copy(update={"intents": intents}),
        }
    )
    jobs = fixture(run_owned=True)[0].evaluation.jobs
    state = state.model_copy(update={"evaluation": core.EvaluationState(jobs=jobs)})
    ref = invocation(spec)
    continuation = core.Continuation(
        continuation_id=core.ContinuationId(root="profiler-wait"),
        invocation=ref,
        next_invocation=ref.model_copy(
            update={"invocation_id": core.InvocationId(root="profiler-resume")}
        ),
        jobs=tuple(row.resource_id for row in jobs if row.resource_id is not None),
        deadline_at=80.0,
        phase=core.ContinuationPhase.WAITING,
    )
    return state, core.TurnObserved(
        invocation=ref, observation=observation, suspension=continuation
    )


def checkpoint_event(
    state: core.CoreState, request: core.SnapshotAndRetainRun
) -> core.RunInvocationCheckpointObserved:
    assert request.request_id is not None
    return core.RunInvocationCheckpointObserved(
        invocation=request.invocation,
        checkpoint_request=request.request_id,
        observation=turn_observation(
            request, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
        ),
        revision=state.run.facts.baseline,
    )


def test_profiler_yield_checkpoint_authorization_and_one_resumed_dispatch() -> None:
    state, event = profiler_yield()
    yielded = reload_step(state, event)
    assert yielded.state.evaluation.continuations == ()
    assert yielded.state.sessions.invocations[0].pending_suspension == event.suspension
    assert yielded.state.run.receipts[0].completion is None
    request = yielded.requests[0]
    assert isinstance(request, core.SnapshotAndRetainRun)
    committed = reload_step(yielded.state, checkpoint_event(state, request))
    assert committed.state.sessions.invocations[0].pending_suspension is None
    assert len(committed.state.sessions.run_checkpoints) == 1
    assert committed.state.run.receipts[0].completion == core.CompletionStatus.SUCCEEDED
    assert len(committed.state.evaluation.continuations) == 1
    assert committed.state.evaluation.continuations[0].phase == core.ContinuationPhase.WAITING
    settled_state = committed.state
    publications = []
    for index in range(len(settled_state.evaluation.jobs)):
        previous = settled_state.evaluation.jobs[index]
        assert previous.observation is not None
        current = previous.model_copy(
            update={
                "status": core.ObservationStatus.SUCCEEDED,
                "terminal": True,
                "observation": previous.observation.model_copy(
                    update={
                        "status": core.ObservationStatus.SUCCEEDED,
                        "terminal": True,
                        "sequence": 2,
                    }
                ),
            }
        )
        incoming = settled_state.model_copy(
            update={
                "evaluation": settled_state.evaluation.model_copy(
                    update={
                        "jobs": tuple(
                            current if row.resource_id == current.resource_id else row
                            for row in settled_state.evaluation.jobs
                        )
                    }
                )
            }
        )
        settled = reload_step(incoming, job_change(current, previous))
        settled_state = settled.state
        publications.extend(row for row in settled.events if isinstance(row, core.ResumeAuthorized))
    wait = settled_state.evaluation.continuations[0]
    assert wait.phase == core.ContinuationPhase.AUTHORIZED
    assert len(publications) == 1
    spec = state.sessions.invocations[0].turn.model_copy(
        update={
            "invocation_id": wait.next_invocation.invocation_id,
            "charge_class": "resume",
            "continuation_id": wait.continuation_id,
        }
    )
    proposal = core.DecisionSubmitted(
        decision=core.RequestTurn(
            decision_id=core.DecisionId(root="profiler-next"), scope=scope(), turn=spec
        ),
        expected_revision=settled_state.revision,
    )
    resumed = reload_step(settled_state, proposal)
    assert len(resumed.requests) == 1
    assert isinstance(resumed.requests[0], core.ResumeSessionTurn)
    assert resumed.requests[0].turn == spec
    assert len(resumed.state.sessions.run_charges) == 2
    assert reload_step(resumed.state, proposal).requests == ()
    assert reload_step(committed.state, checkpoint_event(state, request)).events == ()


@given(
    status=st.sampled_from(list(core.ObservationStatus)),
    accepted=st.booleans(),
    terminal=st.booleans(),
    revision=st.booleans(),
)
def test_run_retention_guard_requires_all_positive_checkpoint_facts(
    status: core.ObservationStatus, *, accepted: bool, terminal: bool, revision: bool
) -> None:
    state, event = profiler_yield()
    yielded = reload_step(state, event)
    request = yielded.requests[0]
    assert isinstance(request, core.SnapshotAndRetainRun)
    observed = checkpoint_event(state, request)
    observed = observed.model_copy(
        update={
            "revision": observed.revision if revision else None,
            "observation": observed.observation.model_copy(
                update={"status": status, "accepted": accepted, "terminal": terminal}
            ),
        }
    )
    result = reload_step(yielded.state, observed)
    proven = status == core.ObservationStatus.SUCCEEDED and accepted and terminal and revision
    assert bool(result.state.sessions.run_checkpoints) == proven
    assert bool(result.state.evaluation.continuations) == proven
    if not proven:
        assert result.state.sessions.invocations[0].pending_suspension == event.suspension
        assert result.state.run.receipts[0].completion is None


@given(order=st.lists(st.sampled_from(["yield", "checkpoint", "foreign"]), min_size=0, max_size=15))
def test_pending_suspension_replays_once_with_reordered_duplicate_and_stale_events(
    order: list[str],
) -> None:
    initial, event = profiler_yield()
    yielded = reload_step(initial, event)
    request = yielded.requests[0]
    assert isinstance(request, core.SnapshotAndRetainRun)
    checkpoint = checkpoint_event(initial, request)
    foreign = checkpoint.model_copy(
        update={
            "checkpoint_request": core.RequestId(root="old-checkpoint"),
            "invocation": checkpoint.invocation.model_copy(update={"generation": 1}),
            "observation": checkpoint.observation.model_copy(
                update={
                    "scope": scope().model_copy(update={"generation": 1}),
                    "request_id": core.RequestId(root="old-checkpoint"),
                }
            ),
        }
    )
    state = yielded.state
    authorized = []
    for tag in (*order, "checkpoint", "yield", "checkpoint"):
        result = reload_step(
            state, {"yield": event, "checkpoint": checkpoint, "foreign": foreign}[tag]
        )
        authorized.extend(row for row in result.events if isinstance(row, core.ResumeAuthorized))
        state = result.state
    assert len(authorized) == 0
    assert state.evaluation.continuations[0].phase == core.ContinuationPhase.WAITING
    assert state.sessions.invocations[0].pending_suspension is None
    assert len(state.sessions.run_checkpoints) == 1
    assert len(state.evaluation.continuations) == 1
    assert state.sessions.run_charges == initial.sessions.run_charges


def run_drain_state() -> tuple[core.CoreState, core.RunSessionsDrainRequested]:
    state, _ = profiler_yield()
    result = core.RunResultProposal(outcome="cancelled", reason="stop")
    decision = core.Stop(
        decision_id=core.DecisionId(root="first-stop"), scope=scope(), mode="cancel", result=result
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=hashlib.sha256(
            json.dumps(
                decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": result,
                    "receipts": (*state.run.receipts, receipt),
                }
            )
        }
    )
    return state, core.RunSessionsDrainRequested(scope=scope(), authority=decision.decision_id)


@given(
    authority=st.booleans(),
    generation=st.booleans(),
    closing=st.booleans(),
    result_matches=st.booleans(),
)
def test_run_drain_requires_first_exact_stop_and_current_closing_scope(
    *, authority: bool, generation: bool, closing: bool, result_matches: bool
) -> None:
    state, event = run_drain_state()
    event = event.model_copy(
        update={
            "authority": event.authority if authority else core.DecisionId(root="foreign"),
            "scope": scope() if generation else scope().model_copy(update={"generation": 1}),
        }
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING if closing else core.RunStatus.RUNNING,
                    "result": state.run.result
                    if result_matches
                    else core.RunResultProposal(outcome="failure", reason="different"),
                }
            )
        }
    )
    result = reload_step(state, event)
    proven = authority and generation and closing and result_matches
    assert any(isinstance(row, core.CancelTurn) for row in result.requests) == proven
    assert result.state.sessions.inputs == state.sessions.inputs
    assert result.state.sessions.run_charges == state.sessions.run_charges
    assert reload_step(result.state, event).requests == ()
