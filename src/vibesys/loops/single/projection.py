"""Persisted run projection for the single-agent strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.loops.agent.readmodel import project_run_view
from vibesys.loops.agent.state import AgentRunState, load_agent_run_state
from vibesys.orchestration.view import RunStatus, RunView

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_project.api import Project


@dataclass(frozen=True, slots=True)
class SingleProjector:
    """Project the strategy's persisted state into the public run view."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project one persisted run into a generic view envelope."""
        state = load_agent_run_state(project, run_id, namespace="single") or AgentRunState()
        return project_run_view(
            state,
            run_id=run_id,
            status=status,
            experiment_revision=state.experiment_revision,
            loop=loop,
        )

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a just-committed strategy state."""
        if namespace != "single" or not isinstance(state, AgentRunState):
            return None
        return project_run_view(
            state,
            run_id=run_id,
            status=RunStatus.ACTIVE,
            experiment_revision=state.experiment_revision,
            loop="single-agent",
        )
