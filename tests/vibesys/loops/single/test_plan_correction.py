"""The designer's plan-ID correction retry (``SingleAgentTurns.plan``): a
plan that reuses an already-used ``hypothesis_id`` is rejected and
reprompted once, in the same turn. If the correction is invalid too, the
original ``InvalidPlanError`` propagates directly from the second
attempt's ``if attempt: raise`` -- the module's ``PlanCorrectionExhaustedError``
fallback below the loop is unreachable through this interface (mirrors
``tests/vibesys/loops/multi/test_plan_correction.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.loops._support import run_agent_loop

from vibesys.errors import InvalidPlanError
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.single.orchestration import SingleAgentOrchestrator
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.schemas import CandidateDisposition
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path


def _options() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions.model_validate(
        {
            "interface": "inprocess",
            "max_rounds": 2,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 1,
            "memory_layout": "files",
            "metric_space": MetricSpace(),
        }
    )


def _plan(hypothesis_id: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040143 [S106]; the argument is a fixture literal, not a credential.
        reasoning="scripted",
    )


def _combined(summary: str) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary=summary,
        expected_behavior="higher steady-state throughput",
        self_review="reviewed the diff and the checks",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="prefill launch overhead dominates at low batch sizes",
        suggestions="batch decode requests next",
        profile_analysis="ran the local checks",
        candidate_disposition=CandidateDisposition.UNASSESSED,
    )


def test_reused_hypothesis_id_is_rejected_and_reprompted_once(tmp_path: Path) -> None:
    """Round 2's designer first reuses round 1's ``H-01`` id (invalid: a
    hypothesis id names one investigation permanently); the correction
    retry proposes ``H-02`` and the round proceeds normally.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        _plan("H-01"),
        _plan("H-01"),  # invalid: reuses round 1's id
        _plan("H-02"),  # the correction
    )
    runner.enqueue(
        "implementer",
        _combined("round 1: batched the prefill step"),
        _combined("round 2: batched the decode step"),
    )

    descriptor = descriptor_from_options(_options(), orchestration_id="single-agent")
    run = run_agent_loop(
        tmp_path, runner, SingleAgentOrchestrator, descriptor, exp_name="plan-correction"
    )

    assert run.result is True
    plan_calls = runner.calls_for("orchestrator")
    # round 1's clean plan, round 2's rejected attempt, round 2's correction.
    assert len(plan_calls) == 3
    assert "rejected" in plan_calls[2].user_prompt or "corrected" in plan_calls[2].user_prompt


def test_correction_exhaustion_raises_when_the_retry_is_also_invalid(tmp_path: Path) -> None:
    """A correction that repeats the same invalid id exhausts the one
    retry the designer gets: the second attempt's ``InvalidPlanError``
    propagates directly, rather than looping forever or being converted
    into a different error.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _plan("H-01"), _plan("H-01"), _plan("H-01"))
    runner.enqueue("implementer", _combined("round 1: batched the prefill step"))

    descriptor = descriptor_from_options(_options(), orchestration_id="single-agent")
    with pytest.raises(InvalidPlanError):
        run_agent_loop(
            tmp_path,
            runner,
            SingleAgentOrchestrator,
            descriptor,
            exp_name="plan-correction-exhausted",
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
