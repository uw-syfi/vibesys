"""The raw provider reply boundary uses Pydantic's JSON validation semantics."""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from vs_agent.api import parse_typed_response


class StrictReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence: tuple[str, ...]


@pytest.mark.parametrize("presentation", ["raw", "fenced", "prose"])
@given(evidence=st.lists(st.text(), max_size=5))
def test_provider_json_preserves_strict_tuple_response(
    presentation: Literal["raw", "fenced", "prose"], evidence: list[str]
) -> None:
    reply = StrictReply(evidence=tuple(evidence))
    text = reply.model_dump_json()
    if presentation == "fenced":
        text = "```json\n" + text + "\n```"
    elif presentation == "prose":
        text = "Result: " + text
    assert parse_typed_response(text, StrictReply) == reply
