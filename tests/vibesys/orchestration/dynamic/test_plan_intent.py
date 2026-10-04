"""Planner decisions declare the intent of each slot before work is dispatched."""

from __future__ import annotations

import pytest
from hypothesis import example, given
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
_UNICODE_TEXT = st.text(min_size=1, max_size=12000).filter(lambda value: bool(value.strip()))
_BLANK = st.text(alphabet=" \t\n\r\u00a0\u2003\u2028\u3000", max_size=30)
_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=40),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=10), children, max_size=3)
    ),
    max_leaves=10,
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
    blank=_BLANK,
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


@given(
    choice=st.sampled_from(
        tuple(
            (kind, field)
            for kind, fields in _REQUIRED.items()
            for field in fields
            if field not in {"hypothesis_id", "profile_id", "target_hypothesis_id", "question"}
        )
    ),
    text=_UNICODE_TEXT,
)
@example(choice=("implement", "task"), text="界🧪" * 5000)
@example(choice=("profile", "decision_impact"), text="界🧪" * 5000)
def test_free_text_roundtrips_without_truncation(choice: tuple[str, str], text: str) -> None:
    kind, field = choice
    entry = dict(_IMPLEMENT if kind == "implement" else _PROFILE)
    entry[field] = text
    parsed = PortfolioPlan.model_validate(_plan(entry))
    restored = PortfolioPlan.model_validate_json(parsed.model_dump_json())

    assert getattr(restored.workstreams[0], field) == text


@given(
    key=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=40).map(
        lambda key: "unknown_" + key
    ),
    value=_JSON_VALUES,
    location=st.sampled_from(("portfolio", "implement", "profile", "evidence", "update")),
)
def test_unknown_keys_are_rejected_at_every_plan_boundary(
    key: str, value: object, location: str
) -> None:
    entry = dict(_PROFILE if location == "profile" else _IMPLEMENT)
    plan = _plan(entry)
    target = plan
    path: tuple[str | int, ...] = ()
    if location in {"implement", "profile"}:
        target = entry
        path = ("workstreams", 0, location)
    elif location == "evidence":
        target = {"location": "candidate.py", "purpose": "implementation evidence"}
        entry["evidence"] = [target]
        path = ("workstreams", 0, "implement", "evidence", 0)
    elif location == "update":
        target = {
            "hypothesis_id": "prior",
            "disposition": "parked",
            "reason_kind": "lower_priority",
            "reason": "Other mechanisms have higher priority.",
        }
        plan["hypothesis_updates"] = [target]
        path = ("hypothesis_updates", 0)
    target[key] = value

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    assert any(
        error["loc"] == (*path, key) and error["type"] == "extra_forbidden"
        for error in rejected.value.errors()
    )


@given(blank=_BLANK)
@example(blank=" ")
def test_blank_portfolio_reasoning_is_rejected_at_its_field(blank: str) -> None:
    plan = _plan(dict(_IMPLEMENT))
    plan["reasoning"] = blank

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    assert any(error["loc"] == ("reasoning",) for error in rejected.value.errors())


@given(blank=_BLANK, field=st.sampled_from(("location", "purpose", "revision")))
@example(blank=" ", field="location")
@example(blank=" ", field="purpose")
@example(blank=" ", field="revision")
def test_blank_evidence_fields_are_rejected_at_the_cited_field(blank: str, field: str) -> None:
    evidence = {"location": "candidate.py", "purpose": "implementation evidence", "revision": "r1"}
    evidence[field] = blank
    entry = dict(_IMPLEMENT)
    entry["evidence"] = [evidence]

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan(entry))

    assert any(
        error["loc"] == ("workstreams", 0, "implement", "evidence", 0, field)
        for error in rejected.value.errors()
    )
