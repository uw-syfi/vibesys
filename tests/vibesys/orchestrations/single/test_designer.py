"""Real single-agent designer policy through the public runtime Fake."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.errors import InvalidPlanError
from vibesys.orchestrations.single import PLUGIN
from vibesys.orchestrations.single.designer import DesignerPlanRequest, request_plan
from vibesys.roles.designer import PlanContext
from vibesys.schemas import SkillResourceSelection
from vibesys.search.hypothesis import (
    Hypothesis,
    HypothesisConfig,
    HypothesisSearch,
    HypothesisState,
    HypothesisStrategyUpdate,
    OrchestratorPlan,
)
from vs_runtime.api import StructuredResponseError
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


def _plan(hypothesis_id: str, **changes: object) -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "hypothesis_id": hypothesis_id,
            "hypothesis": "Batching removes per-request overhead.",
            "title": "Batch prefill",
            "task": "Batch prefill requests.",
            "pass_criteria": "Throughput improves without an accuracy regression.",
            "reasoning": "The trace shows repeated launch overhead.",
            **changes,
        }
    )


def _context() -> PlanContext:
    return PlanContext(
        objective_location="OBJECTIVE.md",
        profiler_summary=None,
        regression_info=None,
        exhaustion_info=None,
        progress_location="progress/ledger.md",
        roadmap_location="progress/roadmap.md",
        pareto_archive_location="progress/pareto.md",
        plateau_warning=None,
        domain_orchestrator="Follow the serving contract.",
        runtime_notes="Use the allocated device.",
        framework_benchmark_enabled=True,
        official_eval_every=3,
        provisional_candidates=1,
        official_eval_cadence_due=False,
    )


def _search() -> HypothesisSearch:
    return HypothesisSearch(HypothesisConfig(max_rounds=3))


class _Script:
    def __init__(self, *replies: object) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[tuple[str, ...], str, type[BaseModel] | None]] = []

    def respond(
        self,
        _role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((history, message, response))
        value = self.replies.popleft()
        if isinstance(value, BaseException):
            raise value
        return value


def _run(
    script: _Script,
    *,
    state: HypothesisState | None = None,
    round_number: int = 1,
    installed: dict[str, tuple[str, ...]] | None = None,
) -> tuple[OrchestratorPlan, FakeRunHost]:
    async def scenario() -> tuple[OrchestratorPlan, FakeRunHost]:
        host = FakeRunHost(PLUGIN, project_root=Path("/candidate"), responder=script.respond)
        host.skills.installed_resources = installed or {}
        request = DesignerPlanRequest(
            round_number=round_number,
            state=state or HypothesisState(),
            context=_context(),
            workspace=host.workspaces.root,
        )
        try:
            return await request_plan(host, _search(), request), host
        finally:
            await host.close()

    return asyncio.run(scenario())


def test_designer_renders_context_normalizes_plan_and_resolves_skills() -> None:
    selected = SkillResourceSelection(
        skill="profiling",
        resource_paths=["references/att.md", "missing.md"],
        purpose="Interpret attribution.",
    )
    script = _Script(
        _plan(
            "  H-02  ",
            title="  Batch   prefill.  ",
            recommended_skills=[selected],
        )
    )

    plan, host = _run(
        script,
        installed={"profiling": ("SKILL.md", "references/att.md")},
    )

    assert plan.hypothesis_id == "H-02"
    assert plan.title == "Batch prefill."
    assert plan.recommended_skills == [
        SkillResourceSelection(
            skill="profiling",
            resource_paths=["references/att.md"],
            purpose="Interpret attribution.",
        )
    ]
    assert script.calls[0][0] == ()
    assert script.calls[0][2] is OrchestratorPlan
    assert "progress/roadmap.md" in script.calls[0][1]
    assert "Use the allocated device." in script.calls[0][1]
    assert len(host.agents.sessions) == 1
    assert host.agents.sessions[0].closed
    assert any("missing.md" in line for line in host.logs)


def test_reused_hypothesis_id_gets_one_correction_in_same_session() -> None:
    previous = Hypothesis(hypothesis_id="H-01", plan=_plan("H-01"), started_round=1)
    state = HypothesisState(hypotheses=[previous])
    script = _Script(_plan("H-01"), _plan("H-02"))

    plan, host = _run(script, state=state, round_number=2)

    assert plan.hypothesis_id == "H-02"
    assert [len(history) for history, _message, _type in script.calls] == [0, 1]
    assert "H-01" in script.calls[1][1]
    assert "previous plan was rejected" in script.calls[1][1]
    assert "You are the Orchestrator" not in script.calls[1][1]
    assert host.agents.sessions[0].closed


def test_invalid_correction_propagates_and_releases_session() -> None:
    invalid = _plan(
        "H-01",
        hypothesis_updates=[
            HypothesisStrategyUpdate(
                hypothesis_id="H-01", disposition="parked", reason="superseded"
            )
        ],
    )
    script = _Script(invalid, invalid.model_copy(deep=True))
    host = FakeRunHost(PLUGIN, responder=script.respond)

    async def scenario() -> None:
        try:
            with pytest.raises(InvalidPlanError, match="new hypothesis"):
                await request_plan(
                    host,
                    _search(),
                    DesignerPlanRequest(
                        round_number=1,
                        state=HypothesisState(),
                        context=_context(),
                        workspace=host.workspaces.root,
                    ),
                )
        finally:
            await host.close()

    asyncio.run(scenario())
    assert len(script.calls) == 2
    assert host.agents.sessions[0].closed


def test_state_dependent_update_is_rejected_before_skill_resolution() -> None:
    invalid = _plan(
        "H-02",
        hypothesis_updates=[
            HypothesisStrategyUpdate(
                hypothesis_id="unknown", disposition="parked", reason="superseded"
            )
        ],
    )
    script = _Script(invalid, _plan("H-02"))

    plan, host = _run(script)

    assert plan.hypothesis_id == "H-02"
    assert len(script.calls) == 2
    assert host.agents.sessions[0].closed


def test_unparseable_plan_uses_policy_fallback() -> None:
    script = _Script(StructuredResponseError("orchestrator", OrchestratorPlan))

    plan, host = _run(script, round_number=3)

    assert plan.hypothesis_id == "hypothesis-0003"
    assert "fallback" in plan.reasoning
    assert len(script.calls) == 1
    assert host.agents.sessions[0].closed


def test_unparseable_correction_uses_policy_fallback() -> None:
    script = _Script(
        _plan(
            "H-01",
            hypothesis_updates=[
                HypothesisStrategyUpdate(
                    hypothesis_id="H-01", disposition="parked", reason="superseded"
                )
            ],
        ),
        StructuredResponseError("orchestrator", OrchestratorPlan),
    )

    plan, host = _run(script, round_number=2)

    assert plan.hypothesis_id == "hypothesis-0002"
    assert "fallback" in plan.reasoning
    assert [len(history) for history, _message, _type in script.calls] == [0, 1]
    assert host.agents.sessions[0].closed
