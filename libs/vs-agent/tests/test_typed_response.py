"""Parsing an agent's structured reply, and naming what is wrong with it."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, Field

from vs_agent.api import AgentOutputSchemaError
from vs_agent.runner import parse_typed_response


class _Plan(BaseModel):
    title: str = Field(max_length=8)
    count: int


@given(title=st.text(min_size=9, max_size=40), count=st.integers())
def test_an_invalid_field_is_named_in_the_error(title: str, count: int) -> None:
    reply = f"Here is the plan:\n```json\n{json.dumps({'title': title, 'count': count})}\n```"

    with pytest.raises(AgentOutputSchemaError) as raised:
        parse_typed_response(reply, _Plan)

    assert raised.value.detail.startswith("title: ")
    assert "8 characters" in raised.value.detail


@given(title=st.text(max_size=8), count=st.integers())
def test_a_valid_reply_parses(title: str, count: int) -> None:
    payload = {"title": title, "count": count}

    assert parse_typed_response(json.dumps(payload), _Plan) == _Plan(**payload)


@pytest.mark.parametrize("reply", ["", "no json here", "{not json}"])
def test_a_reply_without_a_json_object_says_so(reply: str) -> None:
    with pytest.raises(AgentOutputSchemaError, match="no JSON object"):
        parse_typed_response(reply, _Plan)
