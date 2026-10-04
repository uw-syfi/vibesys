"""Pure requester association transitions preserve immutable capture authority."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_evaluation.api import (
    ContentDigest,
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
            state = state.associate(requester)
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
