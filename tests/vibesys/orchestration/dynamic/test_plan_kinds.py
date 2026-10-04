"""A planned workstream is an implement or a profile workstream, chosen by its ``kind``.

The planner's reply is parsed at the plan boundary: each entry validates as
exactly its kind, an entry without a kind is an implement workstream (as plans
written before profile workstreams were), and an unknown key or kind is
rejected at the entry's location for the planner's correction turn.
"""

from __future__ import annotations

import json

import agentshim
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.dynamic.models import (
    DynamicProfile,
    DynamicState,
    PortfolioPlan,
    ProfileDecision,
    ProfilePlan,
    WorkstreamKind,
    WorkstreamPlan,
    planned_id,
)
from vibesys.orchestration.dynamic.profiles import unavailable_profile_fields
from vs_runtime.api import CandidateProfile, CandidateProfileStatus, ProfileField

# Canonical IDs only: test_plan_ids covers the spelling rules.
_IDS = st.text(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.", min_size=1, max_size=24
)
_KINDS = st.sampled_from(["implement", "implicit", "profile"])
_TARGETS = st.one_of(st.none(), _IDS)


def _entry(kind: str, identifier: str, target: str | None) -> dict[str, object]:
    if kind == "profile":
        return {
            "kind": "profile",
            "profile_id": identifier,
            "target_hypothesis_id": target,
            "question": "Where does the time go?",
            "decision_impact": "Prioritize the implementation that removes the dominant cost.",
        }
    entry: dict[str, object] = {
        "hypothesis_id": identifier,
        "title": "Investigate",
        "hypothesis": "A mechanism limits throughput.",
        "task": "Implement and verify it.",
        "pass_criteria": "Throughput improves.",
    }
    # ``implicit`` omits the kind, as a plan written before profiles did.
    return {"kind": "implement", **entry} if kind == "implement" else entry


@st.composite
def _plans(draw: st.DrawFn) -> tuple[list[str], dict[str, object]]:
    identifiers = draw(st.lists(_IDS, min_size=1, max_size=6, unique=True))
    kinds = [draw(_KINDS) for _ in identifiers]
    entries = [
        _entry(kind, identifier, draw(_TARGETS))
        for kind, identifier in zip(kinds, identifiers, strict=True)
    ]
    return kinds, {"reasoning": "r", "workstreams": entries, "hypothesis_updates": []}


@given(_plans())
def test_each_entry_validates_as_its_kind_and_round_trips(
    case: tuple[list[str], dict[str, object]],
) -> None:
    kinds, data = case

    plan = PortfolioPlan.model_validate(data)

    expected = [ProfileDecision if kind == "profile" else WorkstreamPlan for kind in kinds]
    assert [type(item) for item in plan.workstreams] == expected
    entries = data["workstreams"]
    assert isinstance(entries, list)
    assert [planned_id(item) for item in plan.workstreams] == [
        entry.get("profile_id", entry.get("hypothesis_id")) for entry in entries
    ]
    assert PortfolioPlan.model_validate_json(plan.model_dump_json(), strict=True) == plan


@given(_plans(), st.data())
def test_an_unknown_key_is_rejected_at_its_entry(
    case: tuple[list[str], dict[str, object]], data: st.DataObject
) -> None:
    kinds, plan = case
    entries = plan["workstreams"]
    assert isinstance(entries, list)
    position = data.draw(st.integers(0, len(entries) - 1))
    entries[position] = {**entries[position], "request_evaluation": True}

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    kind = "profile" if kinds[position] == "profile" else "implement"
    assert f"workstreams.{position}.{kind}.request_evaluation" in str(rejected.value)


@given(_plans(), st.text(min_size=1).filter(lambda value: value not in {"implement", "profile"}))
def test_an_unknown_kind_is_rejected_naming_the_kinds(
    case: tuple[list[str], dict[str, object]], kind: str
) -> None:
    _, plan = case
    entries = plan["workstreams"]
    assert isinstance(entries, list)
    entries[0] = {**entries[0], "kind": kind}

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    assert rejected.value.errors()[0]["loc"] == ("workstreams", 0)
    assert rejected.value.errors()[0]["type"] == "union_tag_invalid"


def test_the_reply_schema_is_in_the_strict_provider_subset() -> None:
    """Codex's strict structured output rejects ``oneOf``; the union is ``anyOf``."""
    schema = agentshim.normalize(PortfolioPlan.model_json_schema(), agentshim.SchemaDialect.STRICT)

    assert agentshim.dialect_problems(schema, agentshim.SchemaDialect.STRICT) == []


@given(
    st.sampled_from(CandidateProfileStatus),
    st.lists(_IDS, min_size=1, max_size=4, unique=True),
)
def test_a_state_with_profiles_loads_as_the_store_loads_it(
    status: CandidateProfileStatus, identifiers: list[str]
) -> None:
    failed = status is CandidateProfileStatus.FAILED
    profiles = [
        DynamicProfile(
            profile_id=identifier,
            sequence=sequence,
            planning_call=1,
            plan=ProfilePlan(
                kind=WorkstreamKind.PROFILE,
                profile_id=identifier,
                target_hypothesis_id=None,
                question="Where does the time go?",
            ),
            revision="rev",
            outcome=CandidateProfile(
                revision="rev",
                status=status,
                operation_id="op",
                diagnosis=None if failed else "Decode dominates.",
                failure="the profiler turn failed" if failed else None,
            ),
        )
        for sequence, identifier in enumerate(identifiers, start=1)
    ]
    state = DynamicState(profiles=profiles)

    # Persisted profiles predating decision_impact have no such key.
    legacy = state.model_dump(mode="json")
    for profile in legacy["profiles"]:
        profile["plan"].pop("decision_impact")
    loaded = DynamicState.model_validate_json(json.dumps(legacy), strict=True)

    assert loaded == state
    assert loaded.scheduled() == len(identifiers)


@given(
    requested=st.lists(st.sampled_from(ProfileField), unique=True).map(tuple),
    unavailable=st.lists(st.sampled_from(ProfileField), unique=True).map(tuple),
)
def test_only_known_missing_fields_are_removed_from_future_requests(
    requested: tuple[ProfileField, ...], unavailable: tuple[ProfileField, ...]
) -> None:
    prior = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="prior",
        target_hypothesis_id=None,
        question="Measure",
    )
    state = DynamicState(
        profiles=[
            DynamicProfile(
                profile_id="prior",
                sequence=1,
                planning_call=1,
                plan=prior,
                revision="old-revision",
                outcome=CandidateProfile(
                    revision="old-revision",
                    status=CandidateProfileStatus.UNSUPPORTED,
                    missing_fields=unavailable,
                    diagnosis="Unavailable",
                ),
            )
        ]
    )
    plan = ProfilePlan(
        kind=WorkstreamKind.PROFILE,
        profile_id="next",
        target_hypothesis_id=None,
        question="Measure",
        required_fields=requested,
    )
    assert set(unavailable_profile_fields(state, plan)) == set(requested) & set(unavailable)
    assert state.unsupported_profiles() == 1
    assert state.unsupported_profiles(scope="capability") == (0 if unavailable else 1)
