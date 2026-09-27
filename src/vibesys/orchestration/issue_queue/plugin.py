"""Explicit declaration of the issue-queue orchestration."""

from pydantic import BaseModel

from vibesys.orchestration.issue_queue.agents import AGENTS
from vibesys.orchestration.issue_queue.models import IssueQueueOptions, IssueQueueState
from vibesys.orchestration.issue_queue.orchestration import orchestrate
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
)

__all__ = ["PLUGIN"]
