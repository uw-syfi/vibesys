"""Real single-agent designer policy through the public runtime Fake."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.hypothesis import (
    Hypothesis,
    HypothesisConfig,
    HypothesisSearch,
    HypothesisState,
    HypothesisStrategyUpdate,
    InvalidPlanError,
    OrchestratorPlan,
    SkillResourceSelection,
)
from vibesys.orchestration.single import PLUGIN
from vibesys.orchestration.single.designer import DesignerPlanRequest, request_plan
from vibesys.orchestration.single.models import PlanContext
from vs_runtime.api import StructuredResponseError, WorkspaceAccess
from vs_runtime.api.testing import FakeRun

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
        profiler_entry=None,
        regression_entry=None,
        exhaustion_entry=None,
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
) -> tuple[OrchestratorPlan, FakeRun]:
    async def scenario() -> tuple[OrchestratorPlan, FakeRun]:
        run = FakeRun(PLUGIN, project_root=Path("/candidate"), responder=script.respond)
        run.skills.installed_resources = installed or {}
        request = DesignerPlanRequest(
            round_number=round_number,
            state=state or HypothesisState(),
            context=_context(),
            workspace=run.workspaces.root,
        )
        try:
            return await request_plan(run, _search(), request), run
        finally:
            await run.close()

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

    plan, run = _run(
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
    assert len(run.agents.sessions) == 1
    assert run.agents.sessions[0].role.workspace_access is WorkspaceAccess.LIMITED
    assert run.agents.sessions[0].writable_paths == ("progress/roadmap.md",)
    assert run.agents.sessions[0].closed
    assert any("missing.md" in call.message for call in run.observations.calls)


def test_reused_hypothesis_id_gets_one_correction_in_same_session() -> None:
    previous = Hypothesis(hypothesis_id="H-01", plan=_plan("H-01"), started_round=1)
    state = HypothesisState(hypotheses=[previous])
    script = _Script(_plan("H-01"), _plan("H-02"))

    plan, run = _run(script, state=state, round_number=2)

    assert plan.hypothesis_id == "H-02"
    assert [len(history) for history, _message, _type in script.calls] == [0, 1]
    assert "H-01" in script.calls[1][1]
    assert "previous plan was rejected" in script.calls[1][1]
    assert "You are the Orchestrator" not in script.calls[1][1]
    assert run.agents.sessions[0].closed


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
    run = FakeRun(PLUGIN, responder=script.respond)

    async def scenario() -> None:
        try:
            with pytest.raises(InvalidPlanError, match="new hypothesis"):
                await request_plan(
                    run,
                    _search(),
                    DesignerPlanRequest(
                        round_number=1,
                        state=HypothesisState(),
                        context=_context(),
                        workspace=run.workspaces.root,
                    ),
                )
        finally:
            await run.close()

    asyncio.run(scenario())
    assert len(script.calls) == 2
    assert run.agents.sessions[0].closed


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

    plan, run = _run(script)

    assert plan.hypothesis_id == "H-02"
    assert len(script.calls) == 2
    assert run.agents.sessions[0].closed


def test_an_unparseable_plan_is_corrected_in_the_same_conversation() -> None:
    script = _Script(
        StructuredResponseError("orchestrator", OrchestratorPlan, detail="root: bad"),
        _plan("H-01"),
    )

    plan, run = _run(script, round_number=3)

    assert plan.hypothesis_id == "H-01"
    # As in production, the correction continues the conversation that holds
    # the rejected turn.
    assert [len(history) for history, _message, _type in script.calls] == [0, 1]
    assert "Correction required" in script.calls[1][1]
    assert "root: bad" in script.calls[1][1]
    assert run.agents.sessions[0].closed


def _run_failing(script: _Script, *, round_number: int) -> tuple[StructuredResponseError, FakeRun]:
    async def scenario() -> tuple[StructuredResponseError, FakeRun]:
        run = FakeRun(PLUGIN, project_root=Path("/candidate"), responder=script.respond)
        request = DesignerPlanRequest(
            round_number=round_number,
            state=HypothesisState(),
            context=_context(),
            workspace=run.workspaces.root,
        )
        try:
            with pytest.raises(StructuredResponseError) as raised:
                await request_plan(run, _search(), request)
            return raised.value, run
        finally:
            await run.close()

    return asyncio.run(scenario())


def test_unparseable_plan_ends_the_run_without_fabricating_one() -> None:
    script = _Script(
        StructuredResponseError("orchestrator", OrchestratorPlan),
        StructuredResponseError("orchestrator", OrchestratorPlan),
    )

    error, run = _run_failing(script, round_number=3)

    assert "OrchestratorPlan" in str(error)
    assert len(script.calls) == 2
    assert run.agents.sessions[0].closed


def test_unparseable_correction_ends_the_run_without_fabricating_a_plan() -> None:
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
        StructuredResponseError("orchestrator", OrchestratorPlan),
    )

    _error, run = _run_failing(script, round_number=2)

    assert [len(history) for history, _message, _type in script.calls] == [0, 1, 2]
    assert run.agents.sessions[0].closed
