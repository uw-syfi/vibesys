"""Combined implementer, reviewer, and profiler policy for the single strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.hypothesis import SkillResourceSelection
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.agents import IMPLEMENTER
from vibesys.orchestration.single.models import (
    SingleAgentRoundContext,
    SingleAgentRoundResponse,
)
from vibesys.orchestration.single.prompts import (
    render_archive_conflict,
    render_pareto_guard,
    render_single_agent_prompt,
)
from vibesys.orchestration.structured_turn import TurnFailed, attempt_structured_turn
from vs_runtime.api import Run, SkillCatalogError, SkillResourceRequest

if TYPE_CHECKING:
    from vibesys.orchestration.hypothesis import AttemptState, HypothesisSearch, OrchestratorPlan
    from vibesys.orchestration.hypothesis.state import RoundRecord
    from vs_runtime.api import AgentBinding, AgentSession, Workspace


@dataclass(frozen=True, slots=True)
class CombinedTurnRequest:
    """Explicit run, attempt, and archive evidence for one combined turn."""

    round_number: int
    plan: OrchestratorPlan
    attempt: AttemptState
    records: tuple[RoundRecord, ...]
    context: SingleAgentRoundContext
    workspace: Workspace

    def __post_init__(self) -> None:
        """Reject identifiers that cannot anchor a stable conversation."""
        if self.round_number < 1:
            message = "combined round_number must be positive"
            raise ValueError(message)
        if self.attempt.retry < 0:
            message = "combined retry must be nonnegative"
            raise ValueError(message)
        if not self.plan.hypothesis_id.strip():
            message = "combined plan requires a hypothesis_id"
            raise ValueError(message)


async def _resolve_skills(
    run: Run, selections: list[SkillResourceSelection]
) -> list[SkillResourceSelection]:
    if not selections:
        return []
    requests = tuple(
        SkillResourceRequest(
            name=item.skill,
            resource_paths=tuple(item.resource_paths),
            purpose=item.purpose,
        )
        for item in selections
    )
    try:
        result = await run.skills.resolve(requests)
    except SkillCatalogError as error:
        run.observations.warning(
            f"[skills] ignored recommendations because the catalog is invalid: {error}"
        )
        return []
    for diagnostic in result.diagnostics:
        run.observations.warning(f"[skills] {diagnostic}")
    return [
        SkillResourceSelection(
            skill=item.name,
            resource_paths=[path.removeprefix(f"{item.name}/") for path in item.resource_paths],
            purpose=item.purpose,
        )
        for item in result.resolved
    ]


class SingleAgentWorker:
    """Own one named implementer conversation per hypothesis until closed."""

    def __init__(self, run: Run, search: HypothesisSearch) -> None:
        """Bind run effects and pure search policy without opening a session."""
        self._run = run
        self._search = search
        self._sessions: dict[str, AgentSession] = {}
        self._closed = False

    async def turn(self, request: CombinedTurnRequest) -> SingleAgentRoundResponse | TurnFailed:
        """Return one response, preserving conversation across retries.

        A reply that stays invalid after its correction, or a timed-out turn,
        returns :class:`TurnFailed`; the caller records it as a failed attempt.

        Mutates the supplied plan's resolved skill recommendations. The caller
        owns subsequent artifact persistence and state transitions.
        """
        if self._closed:
            message = "single-agent worker is closed"
            raise RuntimeError(message)
        plan = request.plan
        plan.recommended_skills = await _resolve_skills(self._run, plan.recommended_skills)
        session = self._sessions.get(plan.hypothesis_id)
        if session is None:
            session = await self._run.agents.create_session(
                IMPLEMENTER,
                workspace=request.workspace,
                member_id=plan.hypothesis_id,
            )
            self._sessions[plan.hypothesis_id] = session
        elif session.workspace != request.workspace:
            message = f"hypothesis {plan.hypothesis_id!r} changed workspace"
            raise ValueError(message)
        response = await attempt_structured_turn(
            session, render_single_agent_prompt(request.context), SingleAgentRoundResponse
        )
        if isinstance(response, TurnFailed):
            return response
        response.skill_context_updates = await _resolve_skills(
            self._run, response.skill_context_updates
        )
        if response.skill_context_updates:
            plan.recommended_skills = await _resolve_skills(
                self._run,
                [*plan.recommended_skills, *response.skill_context_updates],
            )
        conflict = self._search.pareto_conflict(
            disposition=response.candidate_disposition,
            metrics=dict(response.candidate_metrics),
            records=request.records,
            space=request.attempt.agent_run_state.metrics,
        )
        if response.verdict is Verdict.PASS and conflict is not None:
            response = response.model_copy(
                update={
                    "self_review": render_pareto_guard(response.self_review, conflict),
                    "feedback": render_archive_conflict(conflict),
                    "verdict": Verdict.FAIL,
                }
            )
        return response

    async def close(self) -> None:
        """Release all owned conversations in reverse creation order, once."""
        if self._closed:
            return
        self._closed = True
        for session in reversed(tuple(self._sessions.values())):
            await session.close()

    def binding(self, hypothesis_id: str) -> AgentBinding:
        """Return runtime attribution for a hypothesis whose turn has started."""
        try:
            return self._sessions[hypothesis_id].binding
        except KeyError as error:
            message = f"hypothesis {hypothesis_id!r} has no implementer session"
            raise ValueError(message) from error


__all__ = ["CombinedTurnRequest", "SingleAgentWorker"]
