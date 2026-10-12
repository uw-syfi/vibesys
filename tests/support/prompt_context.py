"""The template variables the render owner derives from one prompt context.

A prompt test renders a template the way production does: the context's fields plus the
``reply`` variables the run offers its turn, derived from the strategy's reply types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._run import config as dynamic_config

from vibesys.orchestration.dynamic.core_policy.api import reply_schemas as dynamic_schemas
from vibesys.orchestration.evolve.core_policy.api import reply_schemas as evolve_schemas
from vibesys.orchestration.issue_queue.core_policy.api import reply_schemas as issue_queue_schemas
from vibesys.orchestration.issue_queue.strategy.api import IssueQueueConfig
from vibesys.orchestration.multi.core_policy.api import reply_schemas as multi_schemas
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.single.core_policy.api import reply_schemas as single_schemas
from vs_core.api import ArtifactId, ArtifactRef
from vs_prompts.api import reply_fields
from vs_runtime.api import ProfileField
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


def registered_reply_models() -> list[type[BaseModel]]:
    """Every reply model a strategy registers, under each capability variant a run can have."""
    recipe = ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe")
    models = [
        *dynamic_schemas(dynamic_config()).values(),
        *dynamic_schemas(
            dynamic_config(profiling=True, profile_fields=tuple(ProfileField))
        ).values(),
        *dynamic_schemas(dynamic_config(profiling=True)).values(),
        *evolve_schemas().values(),
        *multi_schemas().values(),
        *single_schemas().values(),
    ]
    for domain in ("llm-serving", "generic"):
        models.extend(
            issue_queue_schemas(
                IssueQueueConfig(
                    recipe=recipe,
                    domain_id=domain,
                    max_rounds=1,
                    max_attempts_per_issue=1,
                    max_issues_per_perf_eval=1,
                    max_issues_per_round=1,
                )
            ).values()
        )
    return models
