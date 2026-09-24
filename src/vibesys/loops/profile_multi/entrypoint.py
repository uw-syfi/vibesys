"""ProfileGuidedMultiAgentOrchestrator: explicit round control for one strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.context import RunSetup, RunStartHints
from vibesys.loops.agent.orchestration import compare_resume_descriptors
from vibesys.loops.agent.policy_attempts import AttemptDecision
from vibesys.loops.agent.policy_profile import ProfileGuidedPolicy
from vibesys.loops.agent.session import AgentSession, AgentSessionPolicy
from vibesys.loops.agent.state import AgentRunState
from vibesys.loops.profile_multi.options import ORCHESTRATION_ID, load_options
from vibesys.loops.profile_multi.policy import MultiAgentAttemptPolicy, MultiAgentRoundPreparation
from vibesys.prompts import PROMPTS_DIR

if TYPE_CHECKING:
    from vibesys.orchestration.runtime import RunContext
    from vs_project.api import OrchestrationDescriptor


class ProfileGuidedMultiAgentOrchestrator:
    """Run the profile-guided-multi-agent strategy."""

    orchestration_id = ORCHESTRATION_ID

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        """Validate the descriptor before any resources are opened."""
        self.options = load_options(descriptor)
        self.setup = RunSetup(
            state_namespace="profile_multi",
            state_slots={"state.json": AgentRunState},
            resume_policy=compare_resume_descriptors,
            use_default_agent=True,
            start_hints=RunStartHints(
                max_rounds=self.options.max_rounds,
                expected_roles=("orchestrator", "implementer", "judge"),
            ),
        )

    async def run(self, ctx: RunContext) -> bool:
        """Run bounded rounds and commit each completed policy decision."""
        assert self.options.profile_guided is not None  # noqa: S101  # validated by options
        session = await AgentSession.open(
            ctx,
            self.options,
            AgentSessionPolicy(
                profile=ProfileGuidedPolicy(self.options.profile_guided),
                preparation_factory=MultiAgentRoundPreparation,
                attempt_factory=MultiAgentAttemptPolicy,
                template_dir=PROMPTS_DIR / "loops" / "profile_multi",
                state_namespace="profile_multi",
            ),
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
