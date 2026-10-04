"""Pure requester association transitions preserve immutable capture authority."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_evaluation.api import (
    ContentDigest,
    EvaluationJoinExpiredError,
    EvaluationState,
    EvidenceFingerprints,
    EvidenceKind,
    HandleAccess,
    HandleAssociation,
)


def _capture() -> HandleAccess:
    digest = ContentDigest.sha256(b"capture")
    return HandleAccess(
        handle_id="capture",
        scope_id="a",
        fingerprints=EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
        kinds=(EvidenceKind.ACCURACY,),
        owners=frozenset({"requester:a"}),
        observers=frozenset({"requester:a"}),
        associations=(HandleAssociation(scope_id="a", generation=0, principal_id="requester:a"),),
    )


_Transition = st.tuples(
    st.sampled_from(("join", "cancel")),
    st.sampled_from(("a", "b", "c")),
    st.integers(min_value=0, max_value=2),
)


@given(transitions=st.lists(_Transition, max_size=40))
def test_association_transitions_preserve_capture_owner_and_other_requesters(
    transitions: list[tuple[str, str, int]],
) -> None:
    state = _capture()
    expected = {("a", 0, "requester:a"): True}
    for action, scope, generation in transitions:
        previous = state
        previous_document = previous.model_dump_json()
        if action == "join":
            if state.cancel_pending:
                continue
            principal = f"requester:{scope}"
            requester = HandleAssociation(
                scope_id=scope, generation=generation, principal_id=principal
            )
            state = state.associate(requester, capture_state=EvaluationState.QUEUED)
            expected[(scope, generation, principal)] = True
        else:
            state = state.detach(scope_id=scope)
            expected = {
                identity: active if identity[0] != scope else False
                for identity, active in expected.items()
            }
        assert previous.model_dump_json() == previous_document
        assert state.scope_id == "a"
        assert state.owners == frozenset({"requester:a"})
        assert {
            (item.scope_id, item.generation, item.principal_id): item.active
            for item in state.associations
        } == expected
        assert state.cancel_pending == (not any(expected.values()))


@given(generation=st.integers(min_value=0), active=st.booleans())
def test_duplicate_requester_identity_is_rejected_independently_of_active_state(
    generation: int, *, active: bool
) -> None:
    requester = HandleAssociation(scope_id="a", generation=generation, principal_id="requester:a")
    document = _capture().model_dump()
    document["associations"] = [requester, requester.model_copy(update={"active": active})]
    with pytest.raises(ValidationError, match="requester associations must be unique"):
        HandleAccess.model_validate(document)


def test_requester_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="unknown"):
        HandleAssociation.model_validate({"scope_id": "a", "generation": 0, "unknown": True})


@given(capture_state=st.sampled_from(tuple(EvaluationState)))
def test_requester_admission_rejects_terminal_failed_attempts(
    capture_state: EvaluationState,
) -> None:
    capture = _capture()
    before = capture.model_dump_json()
    requester = HandleAssociation(scope_id="b", generation=0, principal_id="requester:b")
    if capture_state in {
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }:
        with pytest.raises(EvaluationJoinExpiredError) as error:
            capture.associate(requester, capture_state=capture_state)
        assert error.value.handle_id == capture.handle_id
        assert error.value.state is capture_state
    else:
        joined = capture.associate(requester, capture_state=capture_state)
        assert joined.associations[-1] == requester
    assert capture.model_dump_json() == before
