"""Occupied capacity is a unique exact admission episode, never an empty proof."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.proofs import Mismatch, Missing, ProofField, ProofReason, Proven, occupied_episode


@st.composite
def slot_facts(draw: st.DrawFn) -> tuple[core.AdmissionRequest, core.Slot]:
    root = draw(st.text(alphabet="abc123", min_size=1, max_size=12))
    generation = draw(st.integers(min_value=0, max_value=20))
    pools = tuple(
        core.PoolId(root=name)
        for name in draw(
            st.lists(
                st.text(alphabet="xyz456", min_size=1, max_size=8),
                max_size=3,
                unique=True,
            )
        )
    )
    episode = core.DecisionId(root=f"episode:{root}")
    attempt = core.AttemptRef(attempt_id=core.AttemptId(root=root), generation=generation)
    if draw(st.booleans()):
        request = core.AttemptReopenRequest(
            decision_id=episode,
            request_id=core.RequestId(root=f"reopen:{root}"),
            attempt=attempt,
            pools=pools,
        )
    else:
        request = core.AttemptRequest(
            decision_id=episode,
            attempt_id=attempt.attempt_id,
            generation=generation,
            item_id=core.ItemId(root=f"item:{root}"),
            admission_charge=1,
            pools=pools,
        )
    slot = core.Slot(attempt=attempt, admission_id=episode, pools=pools, admitted_at=0.0)
    return request, slot


@pytest.mark.parametrize(
    "field",
    [
        "exact",
        "absent",
        "duplicate",
        "conflicting_generation",
        "conflicting_admission",
        "target",
        "generation",
        "admission",
        "pools",
        "duplicate_pools",
        "unrelated_only",
    ],
)
@given(facts=slot_facts())
def test_occupied_episode_proves_every_exact_capacity_fact(
    field: str,
    facts: tuple[core.AdmissionRequest, core.Slot],
) -> None:
    request, canonical = facts
    slots = (canonical,)
    expected = Proven(canonical)
    if field == "absent":
        slots = ()
        expected = Missing(ProofReason.ABSENT_RESOURCE)
    elif field in ("duplicate", "conflicting_generation", "conflicting_admission"):
        extra = canonical
        if field == "conflicting_generation":
            extra = extra.model_copy(
                update={
                    "attempt": extra.attempt.model_copy(
                        update={"generation": extra.attempt.generation + 1}
                    )
                }
            )
        elif field == "conflicting_admission":
            extra = extra.model_copy(update={"admission_id": core.DecisionId(root="foreign")})
        slots = (canonical, extra)
        expected = Mismatch(ProofField.MANIFEST)
    elif field != "exact":
        wrong, expected = _wrong_slot(canonical, field)
        slots = (wrong,)
    assert occupied_episode(slots, request) == expected
    assert occupied_episode(tuple(reversed(slots)), request) == expected


def _wrong_slot(slot: core.Slot, field: str) -> tuple[core.Slot, Missing | Mismatch]:
    if field == "target":
        return slot.model_copy(
            update={
                "attempt": slot.attempt.model_copy(
                    update={"attempt_id": core.AttemptId(root="foreign")}
                )
            }
        ), Mismatch(ProofField.SCOPE)
    if field == "generation":
        return slot.model_copy(
            update={
                "attempt": slot.attempt.model_copy(
                    update={"generation": slot.attempt.generation + 1}
                )
            }
        ), Mismatch(ProofField.GENERATION)
    if field == "admission":
        return slot.model_copy(update={"admission_id": core.DecisionId(root="foreign")}), Mismatch(
            ProofField.ADMISSION_ID
        )
    if field in ("pools", "duplicate_pools"):
        pools = (
            (*slot.pools, core.PoolId(root="foreign"))
            if field == "pools"
            else (core.PoolId(root="foreign"),) * 2
        )
        return slot.model_copy(update={"pools": pools}), Mismatch(ProofField.MANIFEST)
    return slot.model_copy(
        update={
            "attempt": slot.attempt.model_copy(
                update={"attempt_id": core.AttemptId(root="foreign")}
            ),
            "admission_id": core.DecisionId(root="foreign"),
        }
    ), Missing(ProofReason.ABSENT_RESOURCE)


@given(facts=slot_facts())
def test_occupied_episode_ignores_independent_other_owners_and_allows_optional_pools(
    facts: tuple[core.AdmissionRequest, core.Slot],
) -> None:
    request, slot = facts
    request = request.model_copy(update={"pools": ()})
    slot = slot.model_copy(update={"pools": ()})
    unrelated, _ = _wrong_slot(slot, "unrelated_only")
    assert occupied_episode((unrelated, slot), request) == Proven(slot)
    assert occupied_episode((slot, unrelated), request) == Proven(slot)


@given(facts=slot_facts())
def test_duplicate_pool_claims_cannot_certify_an_episode(
    facts: tuple[core.AdmissionRequest, core.Slot],
) -> None:
    request, slot = facts
    duplicate = (core.PoolId(root="duplicate"),) * 2
    request = request.model_copy(update={"pools": duplicate})
    slot = slot.model_copy(update={"pools": duplicate})
    assert occupied_episode((slot,), request) == Mismatch(ProofField.MANIFEST)
