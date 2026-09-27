"""Combined implementer, reviewer, and profiler policy for the single strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestrations.single.agents import IMPLEMENTER
from vibesys.orchestrations.single.models import (
    SingleAgentRoundContext,
    SingleAgentRoundResponse,
)
from vibesys.orchestrations.single.prompts import render_single_agent_prompt
from vibesys.schemas import SkillResourceSelection, Verdict
from vs_runtime.api import (
    AgentTurnTimeoutError,
    RunHost,
    SkillCatalogError,
    SkillResourceRequest,
    StructuredResponseError,
)

if TYPE_CHECKING:
    from vibesys.search.hypothesis import AttemptState, HypothesisSearch, OrchestratorPlan
    from vibesys.search.hypothesis.state import RoundRecord
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


def _fallback_response() -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="Single-agent produced no structured response.",
        expected_behavior="unknown",
        self_review="No structured response received.",
        feedback="No structured response received.",
        verdict=Verdict.FAIL,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
    )


def _timeout_response(timeout_seconds: float) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="Single-agent invocation timed out.",
        expected_behavior="unknown",
        self_review=(
            f"The framework stopped the agent after {timeout_seconds:g} seconds "
            "without a structured response."
        ),
        feedback="Inspect retained evidence and return a schema-valid response on retry.",
        verdict=Verdict.FAIL,
        bottlenecks="",
        suggestions="",
        profile_analysis="",
    )


async def _resolve_skills(
    host: RunHost, selections: list[SkillResourceSelection]
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
        result = await host.skills.resolve(requests)
    except SkillCatalogError as error:
        host.log(f"[skills] ignored recommendations because the catalog is invalid: {error}")
        return []
    for diagnostic in result.diagnostics:
        host.log(f"[skills] {diagnostic}")
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

    def __init__(self, host: RunHost, search: HypothesisSearch) -> None:
        """Bind run effects and pure search policy without opening a session."""
        self._host = host
        self._search = search
        self._sessions: dict[str, AgentSession] = {}
        self._closed = False

    async def turn(self, request: CombinedTurnRequest) -> SingleAgentRoundResponse:
        """Return one response, preserving conversation across retries.

        Mutates the supplied plan's resolved skill recommendations. The caller
        owns subsequent artifact persistence and state transitions.
        """
        if self._closed:
            message = "single-agent worker is closed"
            raise RuntimeError(message)
        plan = request.plan
        plan.recommended_skills = await _resolve_skills(self._host, plan.recommended_skills)
        session = self._sessions.get(plan.hypothesis_id)
        if session is None:
            session = await self._host.agents.create_session(
                IMPLEMENTER,
                workspace=request.workspace,
                member_id=plan.hypothesis_id,
            )
            self._sessions[plan.hypothesis_id] = session
        elif session.workspace != request.workspace:
            message = f"hypothesis {plan.hypothesis_id!r} changed workspace"
            raise ValueError(message)
        try:
            response = await session.turn(
                render_single_agent_prompt(request.context),
                response=SingleAgentRoundResponse,
            )
        except StructuredResponseError:
            response = _fallback_response()
        except AgentTurnTimeoutError as error:
            response = _timeout_response(error.timeout_seconds)
        response.skill_context_updates = await _resolve_skills(
            self._host, response.skill_context_updates
        )
        if response.skill_context_updates:
            plan.recommended_skills = await _resolve_skills(
                self._host,
                [*plan.recommended_skills, *response.skill_context_updates],
            )
        conflict = self._search.pareto_conflict(
            disposition=response.candidate_disposition,
            metrics=dict(response.candidate_metrics),
            records=request.records,
            space=request.attempt.agent_run_state.metrics,
        )
        if response.verdict is Verdict.PASS and conflict:
            response = response.model_copy(
                update={
                    "self_review": f"{response.self_review}\n\nFramework Pareto guard: {conflict}",
                    "feedback": conflict,
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
