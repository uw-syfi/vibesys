"""The ``reply`` template variables the render owner derives for a prompt context.

Kept apart from `prompt_context` so a search's prompt tests need only the reply types of
their own search, not those of every search.
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
    variables.pop("template", None)
    return {**variables, **reply_variable(context, replies)}


def profiler_reply() -> ReplyFields:
    """The ``reply`` a profiler turn is offered: the fields of ``ProfilerSummary``."""
    return reply_fields(ProfilerSummary.model_json_schema())
