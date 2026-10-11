"""A reply is either the role's result or a wait for evaluations naming unique handles."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError

from vs_core.api import Waitable, WaitingForEvaluation


class Verdict(BaseModel):
    passed: bool


_REPLY = TypeAdapter(Waitable[Verdict])
_HANDLES = st.lists(st.text("abc123", min_size=1, max_size=4), min_size=1, max_size=4, unique=True)


@given(handles=_HANDLES)
def test_a_wait_parses_as_a_wait(handles: list[str]) -> None:
    reply = _REPLY.validate_python({"kind": "waiting_for_evaluation", "handles": handles})
    assert reply == WaitingForEvaluation(kind="waiting_for_evaluation", handles=tuple(handles))


@given(passed=st.booleans())
def test_a_result_without_a_kind_parses_as_the_result(*, passed: bool) -> None:
    assert _REPLY.validate_json(f'{{"passed": {str(passed).lower()}}}') == Verdict(passed=passed)


@pytest.mark.parametrize("handles", [[], [" "], ["a", "a"], [" a"]])
def test_a_wait_names_nonblank_unique_handles(handles: list[str]) -> None:
    with pytest.raises(ValidationError):
        _REPLY.validate_python({"kind": "waiting_for_evaluation", "handles": handles})
