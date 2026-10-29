"""The designer's reply, ``OrchestratorPlan``, is decoded strictly.

The plan is agent output: a key the schema does not declare is a misspelled field (say
``base_round_number``), and accepting it would run the round on that field's default.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vibesys.hypothesis import HypothesisStrategyUpdate, OrchestratorPlan, SkillResourceSelection

_TEXT = st.text(max_size=12)
_REQUIRED_TEXT = st.text(min_size=1, max_size=12).filter(str.strip)
_IDS = st.from_regex(r"[a-z][a-z0-9]{0,11}", fullmatch=True)

_UPDATES = st.builds(
    HypothesisStrategyUpdate,
    hypothesis_id=_IDS,
    disposition=st.sampled_from(["parked", "abandoned"]),
    reason=_REQUIRED_TEXT,
)
_SKILLS = st.builds(
    SkillResourceSelection,
    skill=_REQUIRED_TEXT,
    resource_paths=st.lists(_TEXT, max_size=2),
    purpose=_REQUIRED_TEXT,
)
_PLANS = st.builds(
    OrchestratorPlan,
    hypothesis_id=_TEXT,
    hypothesis_updates=st.lists(_UPDATES, max_size=2),
    hypothesis=_TEXT,
    title=_TEXT,
    recommended_skills=st.lists(_SKILLS, max_size=2),
    task=_TEXT,
    pass_criteria=_TEXT,
    base_round=st.none() | st.integers(min_value=0, max_value=50),
    reasoning=_TEXT,
)
_UNKNOWN_KEYS = st.from_regex(r"[a-z][a-z_]{0,15}", fullmatch=True).filter(
    lambda key: key not in OrchestratorPlan.model_fields
)


@given(_PLANS)
def test_every_plan_round_trips_through_its_json(plan: OrchestratorPlan) -> None:
    assert OrchestratorPlan.model_validate_json(plan.model_dump_json()) == plan


@given(_PLANS, _UNKNOWN_KEYS, st.booleans() | st.integers() | _TEXT)
def test_a_plan_with_an_undeclared_key_is_rejected_naming_the_key(
    plan: OrchestratorPlan, key: str, value: object
) -> None:
    document = {**plan.model_dump(mode="json"), key: value}
    with pytest.raises(ValidationError) as raised:
        OrchestratorPlan.model_validate(document)
    assert [error["loc"] for error in raised.value.errors()] == [(key,)]
