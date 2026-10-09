"""Every optional Sessions proof combination is exercised through public step."""

from itertools import product

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest
from .test_session_inputs import occurrence
from .test_session_run_authority import profiler_yield
from .test_session_turns import (
    invocation,
    reload_step,
    turn,
    turn_observation,
    waiting_turn_state,
)


@settings(max_examples=3)
@given(sequence=st.integers(min_value=1, max_value=10))
def test_input_delivery_optional_authority_guard_matrix(sequence: int) -> None:
    spec = turn()
    item = occurrence(0, 0)
    state = reload_step(waiting_turn_state(spec), core.SessionInputReceived(input=item)).state
    dispatched = reload_step(state, core.InputReservationRequested(invocation=invocation(spec)))
    request = dispatched.requests[0]
    observation = turn_observation(
        request,
        accepted=True,
        terminal=True,
        status=core.ObservationStatus.SUCCEEDED,
        sequence=sequence,
    )
    for source, recorded, reserved, manifest, digest in product([False, True], repeat=5):
        inv = dispatched.state.sessions.invocations[0].model_copy(
            update={
                "observation": observation if recorded else None,
                "input_ids": (item.input_id,) if manifest else (),
            }
        )
        record = dispatched.state.sessions.inputs[0].model_copy(
            update={"reserved_to": invocation(spec) if reserved else None}
        )
        intents = (
            tuple(
                row.model_copy(update={"payload_digest": row.payload_digest if digest else "wrong"})
                for row in dispatched.state.intents.intents
            )
            if source
            else ()
        )
        candidate = dispatched.state.model_copy(
            update={
                "sessions": dispatched.state.sessions.model_copy(
                    update={"invocations": (inv,), "inputs": (record,)}
                ),
                "intents": dispatched.state.intents.model_copy(update={"intents": intents}),
            }
        )
        result = reload_step(
            candidate,
            core.InputAcceptanceObserved(invocation=invocation(spec), observation=observation),
        )
        proven = source and recorded and reserved and manifest and digest
        assert isinstance(result.state.sessions.inputs[0].receipt, core.InputDelivered) == proven
        assert sum(isinstance(row, core.InputDelivered) for row in result.events) == int(proven)
        assert result.state.sessions.run_charges == candidate.sessions.run_charges
        assert (
            reload_step(
                result.state,
                core.InputAcceptanceObserved(invocation=invocation(spec), observation=observation),
            ).events
            == ()
        )


@settings(max_examples=3)
@given(generation=st.integers(min_value=0, max_value=3))
def test_deferred_suspension_optional_authority_guard_matrix(generation: int) -> None:
    state, event = profiler_yield()
    # Generation is carried by the run and every correlated owner, never inferred.
    if generation:
        run = state.run.model_copy(update={"generation": generation})
        scope_value = core.Scope(owner=run.run_id, generation=generation)
        ref = event.invocation.model_copy(update={"generation": generation})
        wait = event.suspension
        assert wait is not None
        wait = wait.model_copy(
            update={
                "invocation": ref,
                "next_invocation": wait.next_invocation.model_copy(
                    update={"generation": generation}
                ),
            }
        )
        event = event.model_copy(
            update={
                "invocation": ref,
                "suspension": wait,
                "observation": event.observation.model_copy(update={"scope": scope_value}),
            }
        )
        # Canonical payload digests are generated from actual public values below.
        state = state.model_copy(
            update={
                "evaluation": state.evaluation.model_copy(
                    update={
                        "jobs": tuple(
                            row.model_copy(
                                update={
                                    "scope": scope_value,
                                    "observation": row.observation.model_copy(
                                        update={"scope": scope_value}
                                    )
                                    if row.observation is not None
                                    else None,
                                }
                            )
                            for row in state.evaluation.jobs
                        )
                    }
                )
            }
        )
        from_values = state
        inv = from_values.sessions.invocations[0]
        spec = inv.turn.model_copy(update={"workspace": scope_value})
        source = from_values.intents.intents[0]
        request = source.request.model_copy(update={"scope": scope_value, "turn": spec})
        decision = core.RequestTurn(
            decision_id=from_values.run.receipts[0].decision_id, scope=scope_value, turn=spec
        )
        receipt = from_values.run.receipts[0].model_copy(
            update={"decision": decision, "payload_digest": value_digest(decision)}
        )
        state = from_values.model_copy(
            update={
                "run": run.model_copy(update={"receipts": (receipt,)}),
                "sessions": from_values.sessions.model_copy(
                    update={
                        "sessions": (
                            from_values.sessions.sessions[0].model_copy(
                                update={"scope": scope_value, "generation": generation}
                            ),
                        ),
                        "invocations": (
                            inv.model_copy(
                                update={"invocation": ref, "scope": scope_value, "turn": spec}
                            ),
                        ),
                    }
                ),
                "intents": from_values.intents.model_copy(
                    update={
                        "intents": (
                            source.model_copy(
                                update={
                                    "request": request,
                                    "payload_digest": value_digest(request),
                                    "observation": event.observation,
                                }
                            ),
                        )
                    }
                ),
            }
        )
    yielded = reload_step(state, event)
    request = yielded.requests[0]
    assert isinstance(request, core.SnapshotAndRetainRun)
    assert request.request_id is not None
    checkpoint = core.RunInvocationCheckpoint(
        invocation=event.invocation,
        scope=request.scope,
        request_id=request.request_id,
        revision=state.run.facts.baseline,
        retention="wip",
    )
    available = core.InvocationCheckpointAvailable(
        invocation=event.invocation,
        request_id=request.request_id,
        revision=checkpoint.revision,
        retention="wip",
    )
    for retained, source, observation, session, resource in product([False, True], repeat=5):
        rows = tuple(
            row
            for row in yielded.state.intents.intents
            if source or row.request_id == request.request_id
        )
        inv = yielded.state.sessions.invocations[0].model_copy(
            update={"observation": event.observation if observation else None}
        )
        lease = yielded.state.sessions.sessions[0].model_copy(
            update={
                "resource_id": yielded.state.sessions.sessions[0].resource_id if resource else None
            }
        )
        candidate = yielded.state.model_copy(
            update={
                "sessions": yielded.state.sessions.model_copy(
                    update={
                        "run_checkpoints": (checkpoint,) if retained else (),
                        "invocations": (inv,),
                        "sessions": (lease,) if session else (),
                    }
                ),
                "intents": yielded.state.intents.model_copy(update={"intents": rows}),
            }
        )
        result = reload_step(candidate, available)
        proven = retained and source and observation and session and resource
        assert bool(result.state.evaluation.continuations) == proven
        assert (result.state.sessions.invocations[0].pending_suspension is None) == proven
        if proven:
            assert reload_step(result.state, available).events == ()
