"""A prompt-fallback reply is recovered whatever prose surrounds its one JSON object.

Implementer and judge replies are unions whose JSON schema no provider dialect
accepts natively, so every provider answers them through the prompt fallback:
free text that ``parse_typed_response`` must recover the object from. Agents
writing free text mention code, and code has braces.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from vs_agent.api import AgentOutputSchemaError, parse_typed_response


class Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    next_step: str = ""


# Prose without double quotes cannot hold a JSON string, so it holds no object
# that validates as ``Reply``; it may hold braces, as prose about code does.
_PROSE = st.text(
    alphabet=st.sampled_from(list("abc xyz.:;,()[]{}=_\n")),
    max_size=40,
)


@given(
    summary=st.text(max_size=20),
    before=_PROSE,
    after=_PROSE,
)
def test_the_one_valid_object_is_recovered_from_any_surrounding_prose(
    summary: str, before: str, after: str
) -> None:
    reply = Reply(summary=summary)
    text = f"{before}\n{reply.model_dump_json()}\n{after}"

    assert parse_typed_response(text, Reply) == reply


def test_a_brace_in_the_prose_before_the_object_does_not_hide_it() -> None:
    text = 'I replaced the set {} with a list.\n{"summary": "done"}'

    assert parse_typed_response(text, Reply) == Reply(summary="done")


def test_a_lone_closing_brace_after_the_object_does_not_hide_it() -> None:
    text = '{"summary": "done"}\n}'

    assert parse_typed_response(text, Reply) == Reply(summary="done")


@given(first=st.text(max_size=10), last=st.text(max_size=10))
def test_the_last_valid_object_wins_over_an_earlier_fenced_one(first: str, last: str) -> None:
    def fenced(summary: str) -> str:
        return "```json\n" + Reply(summary=summary).model_dump_json() + "\n```"

    text = f"Draft:\n{fenced(first)}\nCorrected:\n{fenced(last)}"

    assert parse_typed_response(text, Reply) == Reply(summary=last)


def test_the_error_names_the_fields_of_the_reply_not_of_a_brace_in_the_prose() -> None:
    text = 'I replaced {} with a list.\n{"summary": 3}'

    with pytest.raises(AgentOutputSchemaError, match=r"summary: .*string") as raised:
        parse_typed_response(text, Reply)

    assert "no JSON object" not in raised.value.detail


def test_prose_without_a_complete_object_is_reported_as_no_json_object() -> None:
    with pytest.raises(AgentOutputSchemaError, match="no JSON object"):
        parse_typed_response("see {the set} and { unclosed", Reply)
