"""Byte-exact agent-facing text owned by the single plugin's prompt templates.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.golden.helpers import assert_exact_text

from vibesys.hypothesis import (
    Hypothesis,
    HypothesisConfig,
    HypothesisSearch,
    HypothesisState,
    HypothesisStrategyUpdate,
    OrchestratorPlan,
)
from vibesys.orchestration.single import PLUGIN
from vibesys.orchestration.single.designer import DesignerPlanRequest, request_plan
from vibesys.orchestration.single.models import PlanContext
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

_FIXTURES = Path(__file__).parent / "fixtures" / "exact_prompts"


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


@pytest.mark.parametrize("agent", PLUGIN.agents, ids=lambda agent: agent.id)
def test_system_prompt_text_is_stable(agent: AgentRole) -> None:
    assert_exact_text(_FIXTURES / f"system-{agent.id}.txt", agent.system_prompt)


@pytest.mark.parametrize("named_updates", [True, False])
def test_plan_correction_message_text_is_stable(*, named_updates: bool) -> None:
    previous = Hypothesis(hypothesis_id="H-01", plan=_plan("H-01"), started_round=1)
    updates = [
        HypothesisStrategyUpdate(hypothesis_id="H-01", disposition="parked", reason="superseded"),
        HypothesisStrategyUpdate(hypothesis_id="H-00", disposition="parked", reason="superseded"),
    ]
    rejected = _plan("H-01", hypothesis_updates=updates if named_updates else [])
    replies = deque([rejected, _plan("H-02")])
    messages: list[str] = []

    def respond(
        _role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        messages.append(message)
        return replies.popleft()

    async def scenario() -> None:
        run = FakeRun(PLUGIN, project_root=Path("/candidate"), responder=respond)
        try:
            await request_plan(
                run,
                HypothesisSearch(HypothesisConfig(max_rounds=3)),
                DesignerPlanRequest(
                    round_number=2,
                    state=HypothesisState(hypotheses=[previous]),
                    context=PlanContext(
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
                    ),
                    workspace=run.workspaces.root,
                ),
            )
        finally:
            await run.close()

    asyncio.run(scenario())

    name = "plan-correction" if named_updates else "plan-correction-no-updates"
    assert_exact_text(_FIXTURES / f"{name}.txt", messages[1])
