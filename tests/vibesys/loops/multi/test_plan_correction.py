"""The designer's plan-ID correction retry (``MultiAgentTurns.plan``): a
plan that reuses an already-used ``hypothesis_id`` is rejected and
reprompted once, in the same turn. If the correction is invalid too, the
original ``InvalidPlanError`` propagates directly from the second
attempt's ``if attempt: raise`` -- the module's ``PlanCorrectionExhaustedError``
fallback below the loop is unreachable through this interface (see the
dead-code note in this file's docstring for ``test_correction_exhaustion_*``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.loops._support import run_agent_loop

from vibesys.errors import InvalidPlanError
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.multi.orchestration import MultiAgentOrchestrator
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path


def _options(**overrides: object) -> AgentOrchestrationOptions:
    values: dict[str, object] = {
        "interface": "inprocess",
        "max_rounds": 2,
        "max_retries_per_round": 2,
        "judge_every": 1,
        "official_eval_every": 1,
        "memory_layout": "files",
        "metric_space": MetricSpace(),
    }
    values.update(overrides)
    return AgentOrchestrationOptions.model_validate(values)


def _decision() -> PreRoundDecision:
    return PreRoundDecision(need_profile=False, profile_focus="", reasoning="scripted")


def _plan(hypothesis_id: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040137 [S106]; the argument is a fixture literal, not a credential.
        reasoning="scripted",
    )


def _implementer() -> ImplementerResponse:
    return ImplementerResponse(
        summary="batched the prefill step",
        expected_behavior="higher steady-state throughput",
        evidence="ran the local checks",
    )


def _judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="reviewed the diff and the checks", feedback="", verdict=Verdict.PASS
    )


def test_reused_hypothesis_id_is_rejected_and_reprompted_once(tmp_path: Path) -> None:
    """Round 2's designer first reuses round 1's ``H-01`` id (invalid: a
    hypothesis id names one investigation permanently); the correction
    retry proposes ``H-02`` and the round proceeds normally.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        _decision(),
        _plan("H-01"),
        _decision(),
        _plan("H-01"),  # invalid: reuses round 1's id
        _plan("H-02"),  # the correction
    )
    runner.enqueue("implementer", _implementer(), _implementer())
    runner.enqueue("judge", _judge(), _judge())

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_agent_loop(
        tmp_path, runner, MultiAgentOrchestrator, descriptor, exp_name="plan-correction"
    )

    assert run.result is True
    plan_calls = runner.calls_for("orchestrator")
    # 2 decisions + 3 plan attempts (1 clean round-1, 1 rejected + 1 corrected for round 2).
    assert len(plan_calls) == 5
    assert "rejected" in plan_calls[4].user_prompt or "corrected" in plan_calls[4].user_prompt


def test_correction_exhaustion_raises_when_the_retry_is_also_invalid(tmp_path: Path) -> None:
    """A correction that repeats the same invalid id exhausts the one
    retry the designer gets: the second attempt's ``InvalidPlanError``
    propagates directly (``if attempt: raise``), rather than looping
    forever or being converted into a different error.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator", _decision(), _plan("H-01"), _decision(), _plan("H-01"), _plan("H-01")
    )
    runner.enqueue("implementer", _implementer())
    runner.enqueue("judge", _judge())

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    with pytest.raises(InvalidPlanError):
        run_agent_loop(
            tmp_path,
            runner,
            MultiAgentOrchestrator,
            descriptor,
            exp_name="plan-correction-exhausted",
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
