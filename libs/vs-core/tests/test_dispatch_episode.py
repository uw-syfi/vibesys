"""A prepared mutation whose attempt episode ended can never be dispatched, so core cancels it.

A stop retires an attempt while requests of its episode wait for a slot. Dispatch authorization
refuses such a request for good (``ordinary mutation requires the current owned admission
episode``). Without a resolver the shell met that refusal as an error and ended the run open.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api import ClockAdvanced, IntentPhase, ObservationStatus, WaitKind, orphan_waits, step

from .test_attempt_retirement import changed, closing_fixture, proof_intent, with_owner

LIVE = (core.AttemptPhase.ACQUIRING, core.AttemptPhase.ACTIVE)
ENDED = (core.AttemptPhase.CLOSING, core.AttemptPhase.TERMINAL)


def _with_prepared_mutation(
    phase: core.AttemptPhase, *, current_admission: bool
) -> tuple[core.CoreState, core.RequestId]:
    """A closing-attempt fixture moved to ``phase`` that also holds a prepared workspace request."""
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0]
    request = core.EnsureWorkspace(
        request_id=core.RequestId(root="late:ensure"),
        scope=core.Scope(owner=ref.attempt_id, generation=ref.generation),
        attempt=ref,
        admission_id=owner.admission_id if current_admission else core.DecisionId(root="earlier"),
        deadline_at=state.run.deadline_at,
        plan=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
    )
    intent = proof_intent(request, state.run.deadline_at, lifecycle=core.LifecycleClass.OWNED_JOB)
    state = with_owner(
        state,
        changed(owner, phase=phase),
        intents=changed(state.intents, intents=(*state.intents.intents, intent)),
    )
    assert request.request_id is not None
    return state, request.request_id


@given(phase=st.sampled_from(ENDED), current_admission=st.booleans())
def test_a_prepared_mutation_of_an_ended_episode_is_cancelled_unsent(
    phase: core.AttemptPhase, *, current_admission: bool
) -> None:
    state, identity = _with_prepared_mutation(phase, current_admission=current_admission)
    assert WaitKind.DISPATCH in {w.waiter for w in orphan_waits(state)}
    result = step(state, ClockAdvanced(now_at=2.0))
    row = next(r for r in result.state.intents.intents if r.request_id == identity)
    assert row.phase == IntentPhase.COMPLETED
    assert row.observation is not None
    assert row.observation.status == ObservationStatus.CANCELLED
    assert not row.observation.accepted
    assert WaitKind.DISPATCH not in {w.waiter for w in orphan_waits(result.state)}


@pytest.mark.parametrize("phase", LIVE)
def test_a_prepared_mutation_of_a_live_episode_is_left_for_dispatch(
    phase: core.AttemptPhase,
) -> None:
    state, identity = _with_prepared_mutation(phase, current_admission=True)
    assert WaitKind.DISPATCH not in {
        w.waiter for w in orphan_waits(state) if w.subject == identity.root
    }
    result = step(state, ClockAdvanced(now_at=2.0))
    row = next(r for r in result.state.intents.intents if r.request_id == identity)
    assert row.phase == IntentPhase.PREPARED
