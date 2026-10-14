"""A planned id that is not in its one canonical spelling is refused at its field, and the planner is told.

Past bug: the id "0 " was stored stripped in the search state while the workstream kept "0 ", so
the loop crashed when the round was recorded. The plan now accepts an id only in its canonical
spelling (`AgentId`), so the reply that carries any other is refused at the plan boundary and the
correction turn names the field, the value and the spelling to use. This joins the three owners of
that sentence: the plan model refuses, the agent library reports the refusal by field, and the
correction template shows it.
"""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._replies import implement, plan_reply
from tests.vibesys.orchestration.dynamic.core_policy._contexts import TURN, planner_fields, render

from vibesys.orchestration.dynamic.models import ImplementPortfolioPlan, PortfolioPlan
from vibesys.orchestration.dynamic.search.api import PlannerCorrection, PromptTemplate
from vs_agent.api import AgentOutputSchemaError, parse_typed_response
from vs_core.api.search import ReplyError, ReplyErrorCode
from vs_sim.api.testing import examples

CANONICAL = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=128,
).filter(lambda value: value == value.strip() and unicodedata.is_normalized("NFC", value))
PADDING = st.integers(0, 3).map(lambda count: " " * count)
"""Spaces: the one whitespace an id may hold inside, and so the one a refusal can say to trim."""
FIELDS = ("hypothesis_id", "parent_hypothesis_id")


def refused(reply_type: type[PortfolioPlan], identifier: str, field: str) -> AgentOutputSchemaError:
    """The refusal of a plan that names ``identifier`` in its ``field``."""
    entry = implement("h1") | {field: identifier}
    with pytest.raises(AgentOutputSchemaError) as raised:
        parse_typed_response(plan_reply(entry), reply_type)
    return raised.value


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    identifier=CANONICAL,
    padding=st.tuples(PADDING, PADDING).filter(any),
    field=st.sampled_from(FIELDS),
    reply_type=st.sampled_from([PortfolioPlan, ImplementPortfolioPlan]),
    fields=planner_fields(),
)
def test_an_id_with_padding_is_refused_at_its_field_and_the_correction_says_how_to_spell_it(
    identifier: str,
    padding: tuple[str, str],
    field: str,
    reply_type: type[PortfolioPlan],
    fields: dict[str, object],
) -> None:
    padded = padding[0] + identifier + padding[1]

    error = refused(reply_type, padded, field)

    (violation,) = [item for item in error.violations if item.path[-1] == field]
    assert violation.path[:2] == ("workstreams", 0)
    assert violation.path[-1] == field
    assert f"{padded!r} is not a valid identifier" in violation.message
    assert f"use {identifier!r}" in violation.message
    # The runtime hands the violation to core as a typed reply error; the correction shows it.
    reply_error = ReplyError(
        path=violation.path, code=ReplyErrorCode.INVALID_VALUE, message=violation.message
    )
    problem = {"kind": "unparseable", "errors": (reply_error,)}
    context = PlannerCorrection.model_validate(
        {**fields, "of": TURN, "problem": problem}, strict=False
    )
    text = render(PromptTemplate.PORTFOLIO_CORRECTION, context)
    assert f"{field}: " in text
    assert violation.message in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(identifier=CANONICAL, field=st.sampled_from(FIELDS))
def test_an_id_in_its_canonical_spelling_is_accepted_unchanged(identifier: str, field: str) -> None:
    entry = implement("h1") | {field: identifier}

    plan = parse_typed_response(plan_reply(entry), PortfolioPlan)

    (workstream,) = plan.workstreams
    assert getattr(workstream, field) == identifier
