"""ProfileGuidedSingleAgentOrchestrator: explicit round control for one strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.context import RunSetup, RunStartHints
from vibesys.loops.agent.orchestration import compare_resume_descriptors
from vibesys.loops.agent.policy_attempts import AttemptDecision
from vibesys.loops.agent.state import AgentRunState
from vibesys.loops.profile_single.options import ORCHESTRATION_ID, load_options
from vibesys.loops.profile_single.session import ProfileSingleSession

if TYPE_CHECKING:
    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor


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
