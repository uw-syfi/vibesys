"""Planner decisions declare the intent of each slot before work is dispatched."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.dynamic import PortfolioPlan

_R21_PROFILE: dict[str, object] = {
    "kind": "profile",
    "profile_id": "prefix_cache_correctness_and_reuse",
    "question": "Is this unexpectedly a profile kind?",
    "target_hypothesis_id": None,
}
_IMPLEMENT: dict[str, object] = {
    "kind": "implement",
    "hypothesis_id": "prefix-cache",
    "title": "Exact-prefix cache",
    "hypothesis": "Reusing exact-prefix state removes repeated prefill.",
    "task": "Implement exact-prefix reuse and test cache correctness.",
    "pass_criteria": "Correct outputs and lower repeated-prefix latency.",
}
_PROFILE: dict[str, object] = {
    "kind": "profile",
    "profile_id": "prefill-cost",
    "target_hypothesis_id": None,
    "question": "How much latency is spent recomputing repeated prefixes?",
    "decision_impact": "Choose prefix caching if repeated prefill dominates latency.",
}
_REQUIRED = {
    "implement": ("hypothesis_id", "title", "hypothesis", "task", "pass_criteria"),
    "profile": ("profile_id", "target_hypothesis_id", "question", "decision_impact"),
}
_TEXT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=1, max_size=80).filter(
    lambda value: bool(value.strip())
)


def _plan(entry: dict[str, object]) -> dict[str, object]:
    return {"reasoning": "Reserve slots according to their intended work.", "workstreams": [entry]}


def test_r21_implementation_disguised_as_a_profile_lacks_measurement_intent() -> None:
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(_R21_PROFILE))

    assert rejected.value.errors()[0]["loc"] == ("workstreams", 0, "profile", "decision_impact")
    assert rejected.value.errors()[0]["type"] == "missing"


@given(kind=st.sampled_from(tuple(_REQUIRED)), text=_TEXT, data=st.data())
def test_each_kind_requires_its_intent_fields(kind: str, text: str, data: st.DataObject) -> None:
    entry: dict[str, object] = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    for field in _REQUIRED[kind]:
        if field not in {"hypothesis_id", "profile_id", "target_hypothesis_id"}:
            entry[field] = text
    parsed = PortfolioPlan.model_validate(_plan(entry))
    assert PortfolioPlan.model_validate_json(parsed.model_dump_json()) == parsed

    field = data.draw(st.sampled_from(_REQUIRED[kind]))
    entry.pop(field)
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))
    assert any(
        error["loc"] == ("workstreams", 0, kind, field) and error["type"] == "missing"
        for error in rejected.value.errors()
    )


@given(
    kind=st.sampled_from(tuple(_REQUIRED)),
    data=st.data(),
)
def test_variant_fields_cannot_contradict_the_declared_kind(kind: str, data: st.DataObject) -> None:
    entry: dict[str, object] = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    other = _PROFILE if kind == "implement" else _IMPLEMENT
    field = data.draw(st.sampled_from(tuple(key for key in other if key != "kind")))
    entry[field] = other[field]

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))

    assert any(
        error["loc"] == ("workstreams", 0, kind, field) and error["type"] == "extra_forbidden"
        for error in rejected.value.errors()
    )


@given(
    kind=st.sampled_from(tuple(_REQUIRED)),
    blank=st.text(alphabet=" \t\n", max_size=20),
    data=st.data(),
)
def test_blank_intent_is_rejected_at_the_field(kind: str, blank: str, data: st.DataObject) -> None:
    entry: dict[str, object] = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    fields = tuple(
        field
        for field in _REQUIRED[kind]
        if field not in {"hypothesis_id", "profile_id", "target_hypothesis_id"}
    )
    field = data.draw(st.sampled_from(fields))
    entry[field] = blank

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))

    assert any(error["loc"] == ("workstreams", 0, kind, field) for error in rejected.value.errors())
