"""The template variables the render owner derives from one prompt context.

A prompt test renders a template the way production does: the context's fields plus the
``reply`` variables the run offers its turn, derived from the strategy's reply types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.profilers import ProfilerSummary
from vs_prompts.api import reply_fields
from vs_runtime.api.core import reply_variable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from vs_core.api import SchemaRef
    from vs_prompts.api import ReplyFields


def context_variables(
    context: BaseModel, replies: Mapping[SchemaRef, type[BaseModel]]
) -> dict[str, object]:
    """The context's fields (minus ``template``) and its reply variables."""
    variables: dict[str, object] = {
        name: getattr(context, name) for name in type(context).model_fields
    }
    variables.pop("template")
    return {**variables, **reply_variable(context, replies)}


def profiler_reply() -> ReplyFields:
    """The ``reply`` a profiler turn is offered: the fields of ``ProfilerSummary``."""
    return reply_fields(ProfilerSummary.model_json_schema())
