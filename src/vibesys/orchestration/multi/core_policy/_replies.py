"""Reply types the agent roles answer with, keyed by the schema the strategy declares."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from vibesys.hypothesis import OrchestratorPlan
from vibesys.orchestration.multi.models import ImplementerResponse, JudgeResponse, PreRoundDecision
from vibesys.orchestration.multi.strategy.api import (
    IMPLEMENTER_REPLY,
    JUDGE_REPLY,
    PLANNER_REPLY,
    PRE_ROUND_REPLY,
    PROFILER_REPLY,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vs_core.api import Waitable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import SchemaRef


class ImplementerReplyModel(RootModel[Waitable[ImplementerResponse]]):
    """The implementer's turn ends with a result or a wait for evaluations."""


class JudgeReplyModel(RootModel[Waitable[JudgeResponse]]):
    """The judge's turn ends with a verdict or a wait for evaluations."""


def reply_schemas() -> Mapping[SchemaRef, type[BaseModel]]:
    """The reply type of every schema the strategy asks a role to answer with."""
    return {
        PRE_ROUND_REPLY: PreRoundDecision,
        PROFILER_REPLY: ProfilerSummary,
        PLANNER_REPLY: OrchestratorPlan,
        IMPLEMENTER_REPLY: ImplementerReplyModel,
        JUDGE_REPLY: JudgeReplyModel,
    }


def run_prompt_variables(
    *,
    objective: str,
    environment_notes: str,
    agent_evaluation: bool,
    validation_recipe_schema: str,
) -> dict[str, object]:
    """The run-level variables every multi template reads, besides its own context's fields."""
    return {
        "objective": objective,
        "environment_notes": environment_notes,
        "agent_evaluation": agent_evaluation,
        "validation_recipe_schema": validation_recipe_schema,
    }
