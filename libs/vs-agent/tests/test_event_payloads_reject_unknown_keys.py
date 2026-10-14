"""Agent event payloads read back from a recorded event reject keys they do not declare."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

from vs_agent.api import (
    AgentStatusData,
    CommandResultPayload,
    JsonResultPayload,
    TodoItemData,
    ToolResultPayload,
)

_JSON_SCALARS = st.none() | st.booleans() | st.integers() | st.text(max_size=8)
_TOOL_RESULTS: st.SearchStrategy[BaseModel] = st.builds(CommandResultPayload) | st.builds(
    JsonResultPayload,
    value=st.dictionaries(st.text(max_size=8), _JSON_SCALARS, max_size=3)
    | st.lists(_JSON_SCALARS, max_size=3),
)
_PAYLOADS: st.SearchStrategy[BaseModel] = st.one_of(
    st.builds(AgentStatusData), _TOOL_RESULTS, st.builds(TodoItemData)
)


@given(payload=_PAYLOADS, key=st.text(min_size=1, max_size=12), value=_JSON_SCALARS)
def test_a_payload_with_an_unknown_key_is_rejected_by_name(
    payload: BaseModel, key: str, value: object
) -> None:
    record = payload.model_dump(mode="json")
    if key in record:
        return
    assert type(payload).model_validate(record) == payload
    with pytest.raises(ValidationError) as rejected:
        type(payload).model_validate({**record, key: value})
    assert [(error["type"], error["loc"]) for error in rejected.value.errors()] == [
        ("extra_forbidden", (key,))
    ]


@given(
    payload=_TOOL_RESULTS,
    key=st.text(min_size=1, max_size=12),
)
def test_a_tool_result_with_an_unknown_key_is_rejected_through_the_union(
    payload: BaseModel, key: str
) -> None:
    record = payload.model_dump(mode="json")
    if key in record:
        return
    with pytest.raises(ValidationError) as rejected:
        TypeAdapter(ToolResultPayload).validate_python({**record, key: None})
    assert {error["type"] for error in rejected.value.errors()} == {"extra_forbidden"}
    assert all(error["loc"][-1] == key for error in rejected.value.errors())
