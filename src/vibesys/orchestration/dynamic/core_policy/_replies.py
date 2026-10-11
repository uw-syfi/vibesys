"""Reply types the agent roles answer with, keyed by the schema the strategy declares."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.models import (
    ImplementerResult,
    ImplementPortfolioPlan,
    PortfolioPlan,
    ReviewResult,
)
from vibesys.orchestration.dynamic.strategy.api import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PLANNER_REPLY,
    PROFILER_REPLY,
)
from vs_core.api import WaitableReply

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from vibesys.orchestration.dynamic.strategy.api import DynamicConfig
    from vs_core.api import SchemaRef


class ImplementerReplyModel(WaitableReply[ImplementerResult]):
    """An implementer or profiler turn ends with a result or a wait for evaluations."""


class JudgeReplyModel(WaitableReply[ReviewResult]):
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


def run_prompt_variables(
    *, objective: str, environment_notes: str, agent_evaluation: bool
) -> dict[str, object]:
    """The run-level variables every dynamic template reads, besides its own context's fields.

    The plan carries this mapping and the renderer adds it to each prompt, so a template
    that reads a new run-level variable gets it from here for production and for tests alike.
    """
    return {
        "objective": objective,
        "environment_notes": environment_notes,
        "agent_evaluation": agent_evaluation,
        "plan_example": planner_reply_example(),
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
