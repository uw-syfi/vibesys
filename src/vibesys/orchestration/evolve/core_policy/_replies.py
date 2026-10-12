"""Reply types the agent roles answer with, keyed by the schema the strategy declares."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, RootModel

from vibesys.orchestration.evolve.models import JudgeResponse, MutatorResponse
from vibesys.orchestration.evolve.strategy.api import JUDGE_REPLY, MUTATOR_REPLY, PROFILER_REPLY
from vibesys.orchestration.profilers import ProfilerSummary
from vs_core.api import Waitable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_core.api import SchemaRef


class MutatorReplyModel(RootModel[Waitable[MutatorResponse]]):
    """The mutator's turn ends with a result or a wait for evaluations."""


class JudgeReplyModel(RootModel[Waitable[JudgeResponse]]):
    """The judge's turn ends with a verdict or a wait for evaluations."""


def reply_schemas() -> Mapping[SchemaRef, type[BaseModel]]:
    """The reply type of every schema the strategy asks a role to answer with."""
    return {
        MUTATOR_REPLY: MutatorReplyModel,
        JUDGE_REPLY: JudgeReplyModel,
        PROFILER_REPLY: ProfilerSummary,
    }


def run_prompt_variables(
    *, objective: str, environment_notes: str, agent_evaluation: bool
) -> dict[str, object]:
    """The run-level variables every evolve template reads, besides its own context's fields."""
    return {
        "objective": objective,
        "environment_notes": environment_notes,
        "agent_evaluation": agent_evaluation,
    }
