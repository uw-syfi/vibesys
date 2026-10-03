"""Profile workstreams: one profiler operation on an existing candidate revision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.prompts import render_profile_request

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from vibesys.orchestration.dynamic.models import DynamicState, ProfilePlan
    from vs_runtime.api import Run


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

    async def execute(self, plan: ProfilePlan) -> None:
        """Profile the scheduled revision and record the typed outcome."""
        index = profile_index(self.state, plan.profile_id)
        item = self.state.profiles[index]
        if item.outcome is not None:
            return
        outcome = await self.run.evaluation.profile(
            item.revision,
            render_profile_request(question=plan.question, objective=self.run.facts.objective),
            member_id=plan.profile_id,
        )
        async with self.lock:
            current = self.state.profiles[index]
            self.state.profiles[index] = current.model_copy(update={"outcome": outcome}, deep=True)
            await self.commit(f"dynamic: profile {plan.profile_id} {outcome.status.value}")


def profile_index(state: DynamicState, profile_id: str) -> int:
    """Return the position of ``profile_id`` in the durable profile list."""
    return next(index for index, item in enumerate(state.profiles) if item.profile_id == profile_id)


__all__ = ["Profiles", "profile_index"]
