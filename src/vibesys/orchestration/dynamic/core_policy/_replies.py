"""Reply types the agent roles answer with, keyed by the schema the strategy declares."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from vibesys.orchestration.dynamic.models import (
    ImplementerReply,
    ImplementPortfolioPlan,
    JudgeReply,
    PortfolioPlan,
)
from vibesys.orchestration.dynamic.strategy.api import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PLANNER_REPLY,
    PROFILER_REPLY,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.orchestration.dynamic.strategy.api import DynamicConfig
    from vs_core.api import SchemaRef


class ImplementerReplyModel(RootModel[ImplementerReply]):
    """An implementer or profiler turn ends with a result or a wait for evaluations."""


class JudgeReplyModel(RootModel[JudgeReply]):
    """A judge turn ends with a verdict or a wait for evaluations."""


def reply_schemas(config: DynamicConfig) -> Mapping[SchemaRef, type[BaseModel]]:
    """The reply type of every schema the strategy asks a role to answer with.

    The planner's schema does not list the revisions it may select; the strategy
    rejects an unknown revision with a correction naming the alternatives.
    """
    return {
        PLANNER_REPLY: PortfolioPlan if config.profiling else ImplementPortfolioPlan,
        IMPLEMENTER_REPLY: ImplementerReplyModel,
        PROFILER_REPLY: ImplementerReplyModel,
        JUDGE_REPLY: JudgeReplyModel,
    }


def planner_reply_example() -> str:
    """A valid planner reply with placeholder values, rendered from the reply model itself.

    The planner prompt shows it so the model copies the shape of every field instead of
    guessing it. It is built from the model, so a renamed or newly required field breaks
    this function rather than leaving the prompt describing a reply the schema rejects.
    """
    ordinals = ("first", "second")
    plan = ImplementPortfolioPlan.model_validate(
        {
            "reasoning": "Why these workstreams test different mechanisms.",
            "workstreams": [
                {
                    "hypothesis_id": f"{ordinal}-idea",
                    "title": f"Name of the {ordinal} goal",
                    "hypothesis": f"The claim the {ordinal} workstream tests.",
                    "task": f"The concrete changes the {ordinal} implementer makes.",
                    "pass_criteria": f"Observable evidence that the {ordinal} goal is met.",
                }
                for ordinal in ordinals
            ],
            "hypothesis_updates": [],
        }
    )
    return plan.model_dump_json(indent=2)
