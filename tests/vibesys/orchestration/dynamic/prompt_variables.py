"""The variables the render owner gives a dynamic template for one prompt context.

A prompt test renders a template the way the production owner does: the context's fields
plus ``reply``, the fields the run offers the context's turn. A planner prompt that offers
profile work is rendered against the reply schema of a run whose capture supplies exactly
the prompt's ``profile_fields``, so a template naming a field the run does not offer fails
the render here, as it would in the run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.prompt_context import context_variables as variables_of
from tests.vibesys.orchestration.dynamic.strategy._run import config

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas
from vibesys.orchestration.dynamic.strategy.api import PlannerCorrectionPrompt, PlannerPrompt

if TYPE_CHECKING:
    from pydantic import BaseModel


def context_variables(context: BaseModel) -> dict[str, object]:
    """The context's fields (minus ``template``) and its ``reply``, as production renders."""
    planner = context.planner if isinstance(context, PlannerCorrectionPrompt) else context
    offer = (
        {"profiling": True, "profile_fields": planner.profile_fields}
        if isinstance(planner, PlannerPrompt) and planner.profiling
        else {}
    )
    return variables_of(context, reply_schemas(config(**offer)))
