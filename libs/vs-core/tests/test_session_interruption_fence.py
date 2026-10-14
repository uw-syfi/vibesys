"""Interruption authority fences every same-session successor through public step."""

from typing import Literal

import pytest

import vs_core.api as core

from .guard_rows import guard_rows
from .test_session_turns import (
    attempt_turn_dispatch_state,
    interrupted_attempt_state,
    invocation,
    reload_state,
)


def _assert_admitted_then_stalled(reached: core.Transition, spec: core.TurnSpec) -> None:
    """The fence admitted *spec*; with no charge granted on an idle session, nothing carries it.

    The same step ends it as stalled, by name, instead of leaving it acquiring.
    """
    assert reached.state.sessions.invocations[-1].turn == spec
    (ended,) = reached.events
    assert isinstance(ended, core.TurnResult)
    assert "acquisition stalled" in ended.detail


@pytest.mark.parametrize(
    ("phase", "charge", "variant", "predecessor"),
    # The refusal reads the phase, the predecessor and a resume together; the variant is
    # one more condition on the same grant.
    guard_rows(
        ["pending", "draining", "checkpointed", "blocked", "completed"],
        ["paid", "free", "correction", "resume"],
        ["new", "correction", "resume"],
        ["none", "exact", "wrong"],
        positive=("completed", "paid", "new", "exact"),
        joint=(0, 1, 3),
    ),
)
def test_interruption_fence_covers_all_successor_variants(
    phase: str,
    charge: Literal["paid", "free", "correction", "resume"],
    variant: Literal["new", "correction", "resume"],
    predecessor: Literal["none", "exact", "wrong"],
) -> None:
    """Every phase/class/predecessor combination holds at both admission and dispatch."""
    for boundary in ("admission", "dispatch"):
        state, previous, spec, owner_scope = interrupted_attempt_state(phase)
        if variant == "resume":
            # An authorized continuation makes resume independently valid, so a
            # pending interrupt must still win even with no predecessor field.
            resumed, resumed_spec = attempt_turn_dispatch_state("resume")
            prior = resumed.sessions.invocations[0]
            previous = prior.invocation
            claim = state.sessions.interrupts[0].model_copy(update={"invocation": previous})
            state = resumed.model_copy(
                update={
                    "sessions": resumed.sessions.model_copy(
                        update={"invocations": (prior,), "interrupts": (claim,)}
                    )
                }
            )
            spec = resumed_spec
        elif variant == "correction":
            prior = state.sessions.invocations[0]
            ancestor_ref = previous.model_copy(
                update={"invocation_id": core.InvocationId(root="ancestor")}
            )
            ancestor = prior.model_copy(
                update={
                    "invocation": ancestor_ref,
                    "turn": prior.turn.model_copy(
                        update={"invocation_id": ancestor_ref.invocation_id}
                    ),
                }
            )
            corrected = prior.model_copy(
                update={
                    "turn": prior.turn.model_copy(
                        update={"charge_class": "correction", "predecessor": ancestor_ref}
                    )
                }
            )
            state = state.model_copy(
                update={
                    "run": state.run.model_copy(update={"limits": state.run.limits}),
                    "sessions": state.sessions.model_copy(
                        update={"invocations": (ancestor, corrected)}
                    ),
                }
            )
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"limits": state.run.limits})}
        )
        prior_ref = {
            "none": None,
            "exact": previous,
            "wrong": previous.model_copy(update={"invocation_id": core.InvocationId(root="wrong")}),
        }[predecessor]
        spec = spec.model_copy(
            update={"charge_class": charge, "predecessor": prior_ref, "max_turns": 5}
        )
        allowed = (
            phase == "completed"
            and predecessor == "exact"
            and charge != "resume"
            and variant != "resume"
        )
        if boundary == "admission":
            event: core.CoreEvent = core.TurnRequested(
                until=core.Until(at=100.0), scope=owner_scope, turn=spec
            )
            if allowed:
                # Attempts A now admits the successor: it records one TURN charge
                # and the successor invocation starts acquiring.
                reached = core.step(reload_state(state), event)
                assert reached.requests == ()
                assert (
                    reached.state.attempts.attempts[0].charges == state.attempts.attempts[0].charges
                )
                _assert_admitted_then_stalled(reached, spec)
                continue
        else:
            ref = invocation(spec)
            candidate = core.Invocation(
                until=core.Until(at=100.0),
                invocation=ref,
                scope=owner_scope,
                turn=spec,
                phase=core.SessionPhase.ACQUIRING,
            )
            logical = core.ChargeReceipt(
                charge_id=core.ChargeId(root="successor-turn"),
                kind=core.ChargeKind.TURN,
                invocation_id=spec.invocation_id,
                charged=1,
            )
            charges = (logical,)
            if charge == "paid":
                charges += (
                    core.ChargeReceipt(
                        charge_id=core.ChargeId(root="successor-paid"),
                        kind=core.ChargeKind.ATTEMPT,
                        invocation_id=spec.invocation_id,
                        charged=1,
                    ),
                )
            owner = state.attempts.attempts[0]
            session = state.sessions.sessions[0].model_copy(
                update={"invocation": spec.invocation_id}
            )
            state = state.model_copy(
                update={
                    "attempts": core.AttemptsState(
                        attempts=(
                            owner.model_copy(
                                update={
                                    "charges": (
                                        *tuple(
                                            row
                                            for row in owner.charges
                                            if row.invocation_id != spec.invocation_id
                                        ),
                                        *charges,
                                    )
                                }
                            ),
                        )
                    ),
                    "sessions": state.sessions.model_copy(
                        update={
                            "sessions": (session,),
                            "invocations": (*state.sessions.invocations, candidate),
                        }
                    ),
                }
            )
            event = core.TurnInputsReserved(invocation=ref, input_ids=())
            if allowed:
                result = core.step(reload_state(state), event)
                assert len(result.requests) == 1
                assert isinstance(result.requests[0], core.DispatchTurn)
                assert result.requests[0].turn == spec
                continue
        before = state.model_dump_json()
        message = (
            "continuation"
            if phase == "completed"
            and predecessor == "exact"
            and (charge == "resume" or variant == "resume")
            else "exact completed proof"
        )
        with pytest.raises(core.ContractValidationError, match=message):
            core.step(reload_state(state), event)
        assert state.model_dump_json() == before
