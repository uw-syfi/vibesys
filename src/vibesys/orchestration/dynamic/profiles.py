"""Profile workstreams: one profiler operation on an existing candidate revision."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.lifecycle import CompleteIntent, step
from vibesys.orchestration.dynamic.prompts import render_profile_request
from vs_evaluator_protocol.api import ProfileField
from vs_runtime.api import CandidateProfile, CandidateProfileStatus, complete_profile

_CANCELLED = "cancelled by the orchestrator before it ended"

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from vibesys.orchestration.dynamic.models import DynamicState, ProfilePlan
    from vs_runtime.api import ProfileCompletion, Run


@dataclass(slots=True)
class Profiles:
    """Runs profile workstreams against one run's durable state.

    A profile's outcome, including a failed one, is committed when the
    profiler operation ends; a profile without an outcome runs again on resume.
    """

    run: Run
    state: DynamicState
    lock: asyncio.Lock
    commit: Callable[[str], Awaitable[None]]

    def execute(self, plan: ProfilePlan) -> ProfileCompletion:
        """Prepare the capture and atomic completion for the runtime shell."""
        index = profile_index(self.state, plan.profile_id)
        item = self.state.profiles[index]
        required_fields = profile_requirements(plan)

        def capture() -> Awaitable[CandidateProfile]:
            return self.run.evaluation.profile(
                item.revision,
                render_profile_request(
                    question=plan.question,
                    objective=self.run.facts.objective,
                    required_fields=required_fields,
                ),
                member_id=plan.profile_id,
                required_fields=required_fields,
            )

        def record(outcome: CandidateProfile) -> Awaitable[None]:
            current = self.state.profiles[index]
            self.state.profiles[index] = current.model_copy(update={"outcome": outcome}, deep=True)
            return self.commit(f"dynamic: profile {plan.profile_id} {outcome.status.value}")

        return complete_profile(None if item.outcome is not None else capture, self.lock, record)

    async def settle_withdrawn(
        self, plan: ProfilePlan, *, terminal: bool, operation_id: str
    ) -> None:
        """Record a cancelled profile failed; a parked one keeps no outcome and reruns."""
        index = profile_index(self.state, plan.profile_id)
        async with self.lock:
            current = self.state.profiles[index]
            if terminal and current.outcome is None:
                outcome = CandidateProfile(
                    revision=current.revision,
                    status=CandidateProfileStatus.FAILED,
                    failure=_CANCELLED,
                )
                self.state.profiles[index] = current.model_copy(
                    update={"outcome": outcome}, deep=True
                )
            self.state.lifecycle, _ = step(
                self.state.lifecycle, CompleteIntent(operation_id=operation_id)
            )
            await self.commit(f"dynamic: profile {plan.profile_id} withdrawn")


def profile_index(state: DynamicState, profile_id: str) -> int:
    """Return the position of ``profile_id`` in the durable profile list."""
    return next(index for index, item in enumerate(state.profiles) if item.profile_id == profile_id)


def profile_requirements(plan: ProfilePlan) -> tuple[ProfileField, ...]:
    """Include explicit requirements and conservative historical free-text phase/API intent.

    Durable pre-requirements plans contain only a question. Recognize the named
    measurements there so resuming them cannot silently report aggregate data.
    """
    fields = set(plan.required_fields)
    question = plan.question.casefold().replace("pre-fill", "prefill").replace("de-code", "decode")
    words = set(re.findall(r"[a-z0-9]+", question))
    if words & {"prefill", "prefilling"}:
        fields.add(ProfileField.PREFILL_TIMING)
    if words & {"decode", "decoding"}:
        fields.add(ProfileField.DECODE_TIMING)
    if "hip" in words and words & {"api", "apis", "runtime"}:
        fields.add(ProfileField.HIP_API_TIMING)
    return tuple(sorted(fields))


def unavailable_profile_fields(state: DynamicState, plan: ProfilePlan) -> tuple[ProfileField, ...]:
    """Reject fields the configured capture has already reported unavailable.

    The run fixes its capture descriptor. Only descriptor-unsupported fields
    persist across revisions; a failed capture says nothing about support.
    """
    unavailable = {
        field
        for item in state.profiles
        if item.outcome is not None
        for field in item.outcome.missing_fields
    }
    return tuple(sorted(set(profile_requirements(plan)) & unavailable))


__all__ = ["Profiles", "profile_index", "profile_requirements", "unavailable_profile_fields"]
