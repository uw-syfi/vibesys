"""The strategies' agent reply schemas are decoded strictly.

A reply is agent output: a key its schema does not declare is a misspelled field, and
accepting it would run the search on that field's default. Each reply round-trips through
its JSON form, and a reply carrying an undeclared key is rejected at that key.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from vibesys.hypothesis import SkillResourceSelection
from vibesys.orchestration.evolve.models import JudgeResponse as EvolveJudgeResponse
from vibesys.orchestration.evolve.models import MutatorResponse
from vibesys.orchestration.multi.models import JudgeResponse as MultiJudgeResponse
from vibesys.orchestration.multi.models import PreRoundDecision
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict

_TEXT = st.text(max_size=12)
_REQUIRED_TEXT = st.text(min_size=1, max_size=12).filter(str.strip)
_FINITE = st.floats(allow_nan=False, allow_infinity=False)
_SKILLS = st.builds(
    SkillResourceSelection,
    skill=_REQUIRED_TEXT,
    resource_paths=st.lists(_TEXT, max_size=2),
    purpose=_REQUIRED_TEXT,
)
_REPLIES: st.SearchStrategy[BaseModel] = st.one_of(
    st.builds(MutatorResponse, summary=_TEXT, hypothesis=_TEXT, expected_behavior=_TEXT),
    st.builds(
        EvolveJudgeResponse, analysis=_TEXT, feedback=_TEXT, verdict=st.sampled_from(Verdict)
    ),
    st.builds(
        MultiJudgeResponse,
        analysis=_TEXT,
        feedback=_TEXT,
        verdict=st.sampled_from(Verdict),
        skills_used=st.lists(_SKILLS, max_size=2),
    ),
    st.builds(PreRoundDecision, need_profile=st.booleans(), profile_focus=_TEXT, reasoning=_TEXT),
    st.builds(
        ProfilerSummary,
        analysis=_TEXT,
        bottlenecks=_TEXT,
        suggestions=_TEXT,
        perf_metric=st.none() | _FINITE,
        perf_unit=st.none() | _TEXT,
        metrics=st.dictionaries(_REQUIRED_TEXT, _FINITE, max_size=2),
    ),
)
_KEYS = st.from_regex(r"[a-z][a-z_]{0,15}", fullmatch=True)


@given(_REPLIES)
def test_every_reply_round_trips_through_its_json(reply: BaseModel) -> None:
    assert type(reply).model_validate_json(reply.model_dump_json()) == reply


@given(_REPLIES, _KEYS, st.booleans() | st.integers() | _TEXT)
def test_a_reply_with_an_undeclared_key_is_rejected_naming_the_key(
    reply: BaseModel, key: str, value: object
) -> None:
    model = type(reply)
    if key in model.model_fields:
        key = f"{key}_x"
    document = {**reply.model_dump(mode="json"), key: value}
    with pytest.raises(ValidationError) as raised:
        model.model_validate(document)
    assert [error["loc"] for error in raised.value.errors()] == [(key,)]
