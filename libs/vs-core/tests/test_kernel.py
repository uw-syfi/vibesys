"""Kernel invariants through the published value API."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_core.api import (
    EVENT_ROUTES,
    Accepted,
    Area,
    AttemptBudget,
    AttemptId,
    AttemptRef,
    ClockAdvanced,
    DecisionId,
    DecisionSubmitted,
    ItemId,
    Park,
    Rejected,
    RejectionCode,
    RequestId,
    RunResultProposal,
    Scope,
    StartAttempt,
    Stop,
    Withdraw,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    project,
    step,
)


def start(item: str) -> StartAttempt:
    """An opaque attempt proposal, with no product-specific vocabulary."""
    state = initial_state()
    return StartAttempt(
        decision_id=DecisionId(root=item),
        scope=Scope(owner=state.run.run_id, generation=0),
        attempt_id=AttemptId(root=item),
        item_id=ItemId(root=item),
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline),
        budget=AttemptBudget(),
    )


@given(
    st.integers(min_value=0, max_value=1000),
    st.text(alphabet="abcdef0123456789", min_size=1, max_size=20),
)
def test_step_determinism_immutability_and_revision(revision: int, identity: str) -> None:
    state = initial_state().model_copy(update={"revision": revision})
    before = state.model_dump_json()
    event = DecisionSubmitted(decision=start(identity), expected_revision=revision)
    result = step(state, event)
    assert result == step(state, event)
    assert state.model_dump_json() == before
    assert result.state.revision == revision + 1
    assert project(result.state).revision == result.state.revision
    # A fresh start is admitted into the free slot and asks for its workspace.
    assert isinstance(result.events[0], Accepted)
    assert result.events[0].decision_id == event.decision.decision_id
    assert [type(request).__name__ for request in result.requests] == ["EnsureWorkspace"]
    view = project(result.state).scheduling
    assert view.queue == ()
    assert [slot.admission_id for slot in view.slots] == [event.decision.decision_id]


def test_disabled_capability_rejects_before_preparing_or_charging() -> None:
    state = initial_state()
    decision = Withdraw(
        decision_id=DecisionId(root="park"),
        scope=Scope(owner=state.run.run_id, generation=0),
        target=AttemptRef(attempt_id=AttemptId(root="opaque"), generation=0),
        disposition=Park(),
    )
    result = step(state, DecisionSubmitted(decision=decision, expected_revision=0))
    assert isinstance(result.events[0], Rejected)
    assert result.events[0].code == RejectionCode.CAPABILITY
    assert result.state.intents == state.intents
    assert result.state.scheduling == state.scheduling


def test_duplicate_receipt_has_no_callback_redelivery_and_conflict_is_rejected() -> None:
    state = initial_state()
    decision = start("one")
    first = step(state, DecisionSubmitted(decision=decision, expected_revision=0))
    repeated = step(first.state, DecisionSubmitted(decision=decision, expected_revision=0))
    assert repeated.events == ()
    assert repeated.requests == ()
    assert repeated.state.run.receipts == first.state.run.receipts
    conflict = decision.model_copy(update={"item_id": ItemId(root="other")})
    result = step(first.state, DecisionSubmitted(decision=conflict, expected_revision=1))
    assert isinstance(result.events[0], Rejected)
    assert result.events[0].code == RejectionCode.IDENTITY_CONFLICT


def test_strict_values_reject_unknown_keys_and_identity_substitution() -> None:
    with pytest.raises(ValidationError):
        Scope.model_validate(
            {"owner": initial_state().run.run_id, "generation": 0, "extra": "unknown"}
        )
    with pytest.raises(ValidationError):
        AttemptRef.model_validate({"attempt_id": RequestId(root="wrong-domain"), "generation": 0})
    with pytest.raises(ValidationError):
        Scope(owner=initial_state().run.run_id, generation=True)


def test_kernel_clock_routes_to_owning_lane() -> None:
    handler = EVENT_ROUTES[Area.SCHEDULING][ClockAdvanced]
    assert f"{handler.__module__}.{handler.__qualname__}" == "vs_core.scheduling.schedule"


def test_zero_completed_work_cannot_claim_success() -> None:
    state = initial_state()
    decision = Stop(
        decision_id=DecisionId(root="premature"),
        scope=Scope(owner=state.run.run_id, generation=0),
        mode="drain",
        result=RunResultProposal(outcome="success", reason="unproven"),
    )
    result = step(state, DecisionSubmitted(decision=decision, expected_revision=0))
    assert isinstance(result.events[0], Rejected)
    assert result.events[0].code == RejectionCode.EVIDENCE
    assert result.requests == ()
