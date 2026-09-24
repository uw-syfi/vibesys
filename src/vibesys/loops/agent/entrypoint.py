"""Four explicit agent orchestrators with one async execution interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from vibesys.context import RunSetup, RunStartHints
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.model import AgentRunState
from vibesys.loops.agent.orchestration import (
    AgentOrchestrationOptions,
    UnsupportedAgentOrchestrationError,
    compare_resume_descriptors,
    options_from_descriptor,
)
from vibesys.loops.agent.policy_attempts import AttemptDecision
from vibesys.loops.agent.policy_multi import MultiAgentAttemptPolicy, MultiAgentRoundPreparation
from vibesys.loops.agent.policy_profile import PlainProfilePolicy, ProfileGuidedPolicy
from vibesys.loops.agent.policy_single import (
    SingleAgentAttemptPolicy,
    SingleAgentRoundPreparation,
)
from vibesys.loops.agent.read_state import load_agent_run_state
from vibesys.loops.agent.readmodel import project_run_view
from vibesys.loops.agent.session import AgentSession
from vibesys.orchestration.view import RunStatus, RunView

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.loops.agent.policy_rounds import RoundPreparationServices
    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor, Project


class _AgentOrchestrator:
    """Shared descriptor validation and host setup for concrete policies."""

    orchestration_id: ClassVar[str]
    requires_profile: ClassVar[bool] = False

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        if descriptor.id != self.orchestration_id:
            raise UnsupportedAgentOrchestrationError(descriptor.id, descriptor.config_version)
        options = options_from_descriptor(descriptor)
        if (options.profile_guided is not None) != self.requires_profile:
            requirement = "required" if self.requires_profile else "forbidden"
            raise ValueError(  # noqa: TRY003
                f"orchestration {descriptor.id!r} profile_guided options are {requirement}"
            )
        if options.interface not in {"inprocess", "service"}:
            raise ValueError(  # noqa: TRY003
                f"orchestration {descriptor.id!r} has unknown interface {options.interface!r}"
            )
        if options.memory_layout not in issue_board.MEMORY_LAYOUTS:
            raise ValueError(  # noqa: TRY003
                f"orchestration {descriptor.id!r} has unknown memory_layout "
                f"{options.memory_layout!r}"
            )
        self.options: AgentOrchestrationOptions = options
        roles = (
            ("orchestrator", "implementer")
            if descriptor.id.endswith("single-agent")
            else ("orchestrator", "implementer", "judge")
        )
        self.setup = RunSetup(
            state_namespace="agent",
            state_model=AgentRunState,
            resume_policy=compare_resume_descriptors,
            use_default_agent=True,
            start_hints=RunStartHints(max_rounds=options.max_rounds, expected_roles=roles),
        )


def _single_preparation(_: RoundPreparationServices) -> SingleAgentRoundPreparation:
    return SingleAgentRoundPreparation()


class MultiAgentOrchestrator(_AgentOrchestrator):
    """Designer, optional profiler, implementer, and independent judge."""

    orchestration_id = "multi-agent"

    async def run(self, ctx: RunContext) -> bool:
        """Schedule durable multi-agent rounds until the total budget ends."""
        session = await AgentSession.open(
            ctx,
            self.options,
            PlainProfilePolicy(),
            MultiAgentRoundPreparation,
            MultiAgentAttemptPolicy,
        )
        try:
            while session.has_next_round:
                await ctx.control.boundary()
                async with session.round_scope():
                    selected = await session.select_hypothesis()
                    for retry in await session.remaining_attempts(selected):
                        await session.begin_attempt(selected, retry)
                        if not await session.implement(selected):
                            continue
                        decision = await session.review(selected)
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


class SingleAgentOrchestrator(_AgentOrchestrator):
    """Designer and one combined implementation, profile, and review agent."""

    orchestration_id = "single-agent"

    async def run(self, ctx: RunContext) -> bool:
        """Schedule durable combined-agent rounds until the total budget ends."""
        session = await AgentSession.open(
            ctx,
            self.options,
            PlainProfilePolicy(),
            _single_preparation,
            SingleAgentAttemptPolicy,
        )
        try:
            while session.has_next_round:
                await ctx.control.boundary()
                async with session.round_scope():
                    selected = await session.select_hypothesis()
                    for retry in await session.remaining_attempts(selected):
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


class ProfileGuidedMultiAgentOrchestrator(_AgentOrchestrator):
    """Profile-guided hypothesis selection with specialist agent turns."""

    orchestration_id = "profile-guided-multi-agent"
    requires_profile = True

    async def run(self, ctx: RunContext) -> bool:
        """Schedule profile-guided multi-agent rounds."""
        assert self.options.profile_guided is not None  # noqa: S101  # validated by constructor
        session = await AgentSession.open(
            ctx,
            self.options,
            ProfileGuidedPolicy(self.options.profile_guided),
            MultiAgentRoundPreparation,
            MultiAgentAttemptPolicy,
        )
        try:
            while session.has_next_round:
                await ctx.control.boundary()
                async with session.round_scope():
                    selected = await session.select_hypothesis()
                    for retry in await session.remaining_attempts(selected):
                        await session.begin_attempt(selected, retry)
                        if not await session.implement(selected):
                            continue
                        decision = await session.review(selected)
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


class ProfileGuidedSingleAgentOrchestrator(_AgentOrchestrator):
    """Profile-guided hypothesis selection with combined agent turns."""

    orchestration_id = "profile-guided-single-agent"
    requires_profile = True

    async def run(self, ctx: RunContext) -> bool:
        """Schedule profile-guided combined-agent rounds."""
        assert self.options.profile_guided is not None  # noqa: S101  # validated by constructor
        session = await AgentSession.open(
            ctx,
            self.options,
            ProfileGuidedPolicy(self.options.profile_guided),
            _single_preparation,
            SingleAgentAttemptPolicy,
        )
        try:
            while session.has_next_round:
                await ctx.control.boundary()
                async with session.round_scope():
                    selected = await session.select_hypothesis()
                    for retry in await session.remaining_attempts(selected):
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


@dataclass(frozen=True, slots=True)
class AgentProjector:
    """Read the same canonical agent state for live and historical views."""

    orchestration_id: str

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project one persisted agent run into the generic view envelope."""
        state = load_agent_run_state(project, run_id) or AgentRunState()
        return project_run_view(
            state,
            run_id=run_id,
            status=status,
            experiment_revision=state.experiment_revision,
            loop=loop,
        )

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project the same state source immediately after a live commit."""
        if namespace != "agent" or not isinstance(state, AgentRunState):
            return None
        return project_run_view(
            state,
            run_id=run_id,
            status=RunStatus.ACTIVE,
            experiment_revision=state.experiment_revision,
            loop=self.orchestration_id,
        )
