"""Options, projection, and round control for profile guided single."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import AttemptDecision
from vibesys.agent_run.options import (
    AgentOrchestrationOptions,
    UnsupportedAgentOrchestrationError,
    compare_resume_descriptors,
    options_from_descriptor,
)
from vibesys.agent_run.readmodel import project_run_view
from vibesys.agent_run.state import AgentRunState, load_agent_run_state
from vibesys.context import RunSetup, RunStartHints
from vibesys.loops.profile_single.session import ProfileSingleSession
from vibesys.orchestration.view import RunStatus, RunView

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor, Project

ORCHESTRATION_ID = "profile-guided-single-agent"


class InvalidStrategyOptionsError(ValueError):
    """An option is incompatible with this strategy."""

    def __init__(self, field: str, value: object) -> None:
        """Name the incompatible strategy option and its value."""
        super().__init__(f"{ORCHESTRATION_ID}: invalid {field} option {value!r}")


def load_options(descriptor: OrchestrationDescriptor) -> AgentOrchestrationOptions:
    """Validate this strategy's identity and policy-specific settings."""
    if descriptor.id != ORCHESTRATION_ID:
        raise UnsupportedAgentOrchestrationError(descriptor.id, descriptor.config_version)
    options = options_from_descriptor(descriptor)
    if options.profile_guided is None:
        raise InvalidStrategyOptionsError("profile_guided", None)
    if options.interface not in {"inprocess", "service"}:
        raise InvalidStrategyOptionsError("interface", options.interface)
    if options.memory_layout not in issue_board.MEMORY_LAYOUTS:
        raise InvalidStrategyOptionsError("memory_layout", options.memory_layout)
    return options


@dataclass(frozen=True, slots=True)
class ProfileSingleProjector:
    """Project the strategy's persisted state into the public run view."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project one persisted run into a generic view envelope."""
        state = load_agent_run_state(project, run_id, namespace="profile_single") or AgentRunState()
        return project_run_view(
            state,
            run_id=run_id,
            status=status,
            experiment_revision=state.experiment_revision,
            loop=loop,
        )

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a just-committed strategy state."""
        if namespace != "profile_single" or not isinstance(state, AgentRunState):
            return None
        return project_run_view(
            state,
            run_id=run_id,
            status=RunStatus.ACTIVE,
            experiment_revision=state.experiment_revision,
            loop=ORCHESTRATION_ID,
        )


class ProfileGuidedSingleAgentOrchestrator:
    """Run the profile-guided-single-agent strategy."""

    orchestration_id = ORCHESTRATION_ID

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        """Validate the descriptor before any resources are opened."""
        self.options = load_options(descriptor)
        self.setup = RunSetup(
            state_namespace="profile_single",
            state_slots={"state.json": AgentRunState},
            resume_policy=compare_resume_descriptors,
            start_hints=RunStartHints(
                max_rounds=self.options.max_rounds,
                expected_roles=("orchestrator", "implementer"),
            ),
        )

    async def run(self, ctx: RunContext) -> bool:
        """Run bounded rounds and commit each completed policy decision."""
        session = await ProfileSingleSession.open(ctx, self.options)
        try:
            while session.has_next_round:
                await ctx.control.boundary()
                async with session.round_scope():
                    selected = await session.select_hypothesis()
                    for retry in session.remaining_attempts(selected):
                        await session.begin_attempt(selected, retry)
                        decision = await session.combined_turn(selected)
                        if decision is AttemptDecision.FINISH:
                            break
                        if decision is AttemptDecision.OFFICIAL and await session.official_gates(
                            selected
                        ):
                            break
                    await session.commit_round(selected)
            return await session.finish()
        finally:
            await session.close()
