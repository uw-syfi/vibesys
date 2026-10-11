"""Reply types the agent roles answer with, keyed by the schema the strategy declares."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from vibesys.hypothesis import OrchestratorPlan
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.single.strategy.api import IMPLEMENTER_REPLY, PLANNER_REPLY
from vs_core.api import Waitable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import SchemaRef


class ImplementerReplyModel(RootModel[Waitable[SingleAgentRoundResponse]]):
    """The combined turn ends with a result or a wait for evaluations."""


def reply_schemas() -> Mapping[SchemaRef, type[BaseModel]]:
    """The reply type of every schema the strategy asks a role to answer with."""
    return {PLANNER_REPLY: OrchestratorPlan, IMPLEMENTER_REPLY: ImplementerReplyModel}


def run_prompt_variables(
    *, objective: str, environment_notes: str, agent_evaluation: bool
) -> dict[str, object]:
    """The run-level variables every single template reads, besides its own context's fields."""
    return {
        "objective": objective,
        "environment_notes": environment_notes,
        "agent_evaluation": agent_evaluation,
    }
