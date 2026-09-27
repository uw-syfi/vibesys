"""Explicit declaration of the issue-queue orchestration."""

from functools import partial

from pydantic import BaseModel

from vibesys.orchestration.issue_queue.agents import AGENTS
from vibesys.orchestration.issue_queue.models import IssueQueueOptions, IssueQueueState
from vibesys.orchestration.issue_queue.orchestration import orchestrate
from vibesys.orchestration.resume import compare_round_budget, project_round_budget
from vs_runtime.api import OrchestrationPlugin, PluginProjection


def _project(raw_state: BaseModel) -> PluginProjection:
    """Expose the strict aggregate without reconstructing policy state."""
    state = IssueQueueState.model_validate(raw_state)
    return PluginProjection(payload=state.model_dump(mode="json"))


PLUGIN = OrchestrationPlugin(
    id="plain",
    agents=AGENTS,
    options=IssueQueueOptions,
    state=IssueQueueState,
    orchestrate=orchestrate,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="plain",
        options_type=IssueQueueOptions,
    ),
    project_max_rounds=partial(project_round_budget, options_type=IssueQueueOptions),
)

__all__ = ["PLUGIN"]
