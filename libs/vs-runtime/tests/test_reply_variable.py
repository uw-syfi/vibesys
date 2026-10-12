"""A prompt context's ``reply`` offers exactly the fields of the reply type the run registered."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_core.api import RolePromptContext, SchemaRef, Value
from vs_prompts.api import TemplateRenderer, UnknownReplyFieldError
from vs_runtime.api.core import reply_variable

_PLAN = SchemaRef(name="test.plan-reply", version=1)
_WORK = SchemaRef(name="test.work-reply", version=1)
_NAMES = st.lists(
    st.from_regex(r"[a-z][a-z_]{0,8}", fullmatch=True), min_size=1, max_size=5, unique=True
)


class _PlanPrompt(RolePromptContext):
    reply_schema: ClassVar[SchemaRef | None] = _PLAN


class _JudgePrompt(RolePromptContext):
    reply_schema: ClassVar[SchemaRef | None] = _WORK
    described_replies: ClassVar[dict[str, SchemaRef]] = {"plan_reply": _PLAN}


class _ReplylessPrompt(Value):
    note: str = ""


def _reply_type(names: list[str]) -> type[BaseModel]:
    return type("Reply", (BaseModel,), {"__annotations__": dict.fromkeys(names, str)})


@given(offered=_NAMES, withheld=_NAMES)
def test_reply_offers_the_registered_types_fields_and_no_others(
    offered: list[str], withheld: list[str]
) -> None:
    replies = {_PLAN: _reply_type(offered)}
    renderer = TemplateRenderer(Path(__file__).parent)

    variables = reply_variable(_PlanPrompt(), replies)

    assert set(variables["reply"]) == set(offered)
    for name in set(withheld) - set(offered):
        with pytest.raises(UnknownReplyFieldError):
            renderer.render_string("{{ reply." + name + " }}", **variables)


@given(plan=_NAMES, work=_NAMES)
def test_a_prompt_describing_another_turn_gets_that_turns_fields_too(
    plan: list[str], work: list[str]
) -> None:
    replies = {_PLAN: _reply_type(plan), _WORK: _reply_type(work)}

    variables = reply_variable(_JudgePrompt(), replies)

    assert set(variables["reply"]) == set(work)
    assert set(variables["plan_reply"]) == set(plan)


def test_a_context_without_a_reply_schema_gets_no_reply() -> None:
    assert reply_variable(_ReplylessPrompt(), {_PLAN: _reply_type(["a"])}) == {}
    assert reply_variable(RolePromptContext(), {}) == {}


def test_a_reply_schema_the_run_did_not_register_fails_loudly() -> None:
    with pytest.raises(LookupError, match=r"test\.plan-reply"):
        reply_variable(_PlanPrompt(), {})
