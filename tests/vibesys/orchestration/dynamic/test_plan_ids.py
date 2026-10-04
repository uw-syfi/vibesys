"""The planner's hypothesis IDs are parsed into one canonical spelling at the plan boundary.

A plan ID becomes a state key, a workspace and Git ref name, and a lookup ID.
An ID with two spellings (``'0 '`` and the ``'0'`` a consumer strips it to)
once crashed the loop when recording its round, so the plan rejects it, naming
the field and the value for the planner's correction turn.
"""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.orchestration.dynamic.models import PortfolioPlan, planned_id


def _canonical(value: str) -> bool:
    return value == value.strip() and unicodedata.is_normalized("NFC", value)


_IDS = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=120,
).filter(_canonical)
# A character that gives an ID a second, look-alike or normalized-away spelling.
_DEFECTS = st.sampled_from([" ", "\t", "\n", "\x00", "\u00a0", "\u200b", "\ufeff", "\u2028"])


def _workstream(identifier: str) -> dict[str, object]:
    return {
        "hypothesis_id": identifier,
        "title": "Investigate",
        "hypothesis": "A mechanism limits throughput.",
        "task": "Implement and verify it.",
        "pass_criteria": "Throughput improves.",
    }


def _plan(identifier: str, *, update: str | None = None) -> dict[str, object]:
    updates = (
        []
        if update is None
        else [
            {
                "hypothesis_id": update,
                "disposition": "parked",
                "reason_kind": "lower_priority",
                "reason": "r",
            }
        ]
    )
    return {
        "reasoning": "Independent mechanisms.",
        "workstreams": [_workstream(identifier)],
        "hypothesis_updates": updates,
    }


def test_a_trailing_space_id_is_rejected_naming_the_field_and_the_spelling_to_use() -> None:
    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(_plan("0 "))

    message = str(rejected.value)
    assert "workstreams.0.implement.hypothesis_id" in message
    assert "'0 ' is not a valid identifier" in message
    assert "use '0'" in message


@given(identifier=_IDS)
@example(identifier="KV.Cache_v2 / ../Ünïcode")
@example(identifier="H1")
def test_a_canonical_id_reaches_the_plan_and_its_updates_unchanged(identifier: str) -> None:
    plan = PortfolioPlan.model_validate(_plan(identifier, update=identifier))

    assert planned_id(plan.workstreams[0]) == identifier
    assert plan.hypothesis_updates[0].hypothesis_id == identifier


@given(
    identifier=_IDS,
    defect=_DEFECTS,
    edge=st.sampled_from(["start", "end"]),
    field=st.sampled_from(["workstreams", "hypothesis_updates"]),
)
def test_an_id_with_a_second_spelling_is_rejected_where_it_was_written(
    identifier: str, defect: str, edge: str, field: str
) -> None:
    defective = defect + identifier if edge == "start" else identifier + defect
    plan = _plan("H1", update=defective) if field == "hypothesis_updates" else _plan(defective)

    with pytest.raises(ValidationError) as rejected:
        PortfolioPlan.model_validate(plan)

    # A workstream entry is validated as its kind, which the location names.
    location = (
        "workstreams.0.implement.hypothesis_id"
        if field == "workstreams"
        else "hypothesis_updates.0.hypothesis_id"
    )
    assert location in str(rejected.value)
    assert repr(defective) in str(rejected.value)
