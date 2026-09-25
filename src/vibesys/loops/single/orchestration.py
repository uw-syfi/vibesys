"""Options, projection, and round control for the single strategy.

Profiling is a configuration option of this strategy
(``AgentOrchestrationOptions.profile_guided``), not a separate strategy
folder. ``profile-guided-single-agent`` stays registered as a preset: the
same round control and prompts as ``single-agent``, with profiling always
on and its own state namespace/projector so existing runs and option files
keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from vibesys.agent_run import issue_board
from vibesys.context import RunSetup, RunStartHints
from vibesys.errors import InvalidStrategyOptionsError
from vibesys.loops.agent_options import (
    AgentOrchestrationOptions,
    UnsupportedAgentOrchestrationError,
    compare_resume_descriptors,
    options_from_descriptor,
)
from vibesys.loops.hypothesis_readmodel import project_run_view
from vibesys.loops.single.session import SingleSession
from vibesys.orchestration.view import RunStatus, RunView
from vibesys.search.hypothesis.attempts import AttemptDecision
from vibesys.search.hypothesis.state import HypothesisState, load_hypothesis_state

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor, Project

ORCHESTRATION_ID = "single-agent"
PROFILE_ORCHESTRATION_ID = "profile-guided-single-agent"


def load_options(
    descriptor: OrchestrationDescriptor, *, orchestration_id: str, require_profile: bool
) -> AgentOrchestrationOptions:
    """Validate this strategy preset's identity and policy-specific settings."""
    if descriptor.id != orchestration_id:
        raise UnsupportedAgentOrchestrationError(descriptor.id, descriptor.config_version)
    options = options_from_descriptor(descriptor)
    if require_profile and options.profile_guided is None:
        raise InvalidStrategyOptionsError(orchestration_id, "profile_guided", None)
    if not require_profile and options.profile_guided is not None:
        raise InvalidStrategyOptionsError(
            orchestration_id, "profile_guided", options.profile_guided
        )
    if options.interface not in {"inprocess", "service"}:
        raise InvalidStrategyOptionsError(orchestration_id, "interface", options.interface)
    if options.memory_layout not in issue_board.MEMORY_LAYOUTS:
        raise InvalidStrategyOptionsError(orchestration_id, "memory_layout", options.memory_layout)
    return options


@dataclass(frozen=True, slots=True)
class SingleProjector:
    """Project this preset's persisted state into the public run view.

    ``namespace`` and ``orchestration_id`` distinguish the plain and
    profile-guided presets, which persist under separate state namespaces.
    """

    namespace: str = "single"
    orchestration_id: str = ORCHESTRATION_ID

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project one persisted run into a generic view envelope."""
        state = (
            load_hypothesis_state(project, run_id, namespace=self.namespace) or HypothesisState()
        )
        return project_run_view(
            state,
            run_id=run_id,
            status=status,
            experiment_revision=state.experiment_revision,
            loop=loop,
        )

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a just-committed strategy state."""
        if namespace != self.namespace or not isinstance(state, HypothesisState):
            return None
        return project_run_view(
            state,
            run_id=run_id,
            status=RunStatus.ACTIVE,
            experiment_revision=state.experiment_revision,
            loop=self.orchestration_id,
        )


ProfileSingleProjector = SingleProjector


class SingleAgentOrchestrator:
    """Run the single-agent strategy, plain or profile-guided.

    Subclassed by :class:`ProfileGuidedSingleAgentOrchestrator` to register
    the profile-guided preset under its own orchestration ID and state
    namespace; the round control (``run``) is identical.
    """

    orchestration_id: ClassVar[str] = ORCHESTRATION_ID
    state_namespace: ClassVar[str] = "single"
    require_profile: ClassVar[bool] = False

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        """Validate the descriptor before any resources are opened."""
        self.options = load_options(
            descriptor,
            orchestration_id=self.orchestration_id,
            require_profile=self.require_profile,
        )
        self.setup = RunSetup(
            state_namespace=self.state_namespace,
            state_slots={"state.json": HypothesisState},
            resume_policy=compare_resume_descriptors,
            start_hints=RunStartHints(
                max_rounds=self.options.max_rounds,
                expected_roles=("orchestrator", "implementer"),
            ),
            memory_paths=issue_board.declared_memory_paths(),
        )

    async def run(self, ctx: RunContext) -> bool:
        """Run bounded rounds and commit each completed policy decision."""
        session = await SingleSession.open(ctx, self.options)
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


class ProfileGuidedSingleAgentOrchestrator(SingleAgentOrchestrator):
    """Run the single-agent strategy with profiling on (registered preset)."""

    orchestration_id: ClassVar[str] = PROFILE_ORCHESTRATION_ID
    state_namespace: ClassVar[str] = "profile_single"
    require_profile: ClassVar[bool] = True
