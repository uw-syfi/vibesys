"""Sparse-review policy: an off-cadence round with a non-terminal, no-fresh-
evidence outcome skips the independent judge entirely (``AttemptDecision.FINISH``
straight from ``review_due() is False``, before ``self.turns.review`` is ever
called), and an unparseable implementer reply retries within the same
attempt without ever reaching the judge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.loops._support import run_agent_loop

from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.multi.orchestration import MultiAgentOrchestrator
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.schemas import HypothesisOutcome
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path


def _options(**overrides: object) -> AgentOrchestrationOptions:
    values: dict[str, object] = {
        "interface": "inprocess",
        "max_rounds": 2,
        "max_retries_per_round": 2,
        "judge_every": 2,
        "official_eval_every": 1,
        "memory_layout": "files",
        "metric_space": MetricSpace(),
    }
    values.update(overrides)
    return AgentOrchestrationOptions.model_validate(values)


def _decision() -> PreRoundDecision:
    return PreRoundDecision(need_profile=False, profile_focus="", reasoning="scripted")


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040142 [S106]; the argument is a fixture literal, not a credential.
        reasoning="scripted",
    )


def _judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="reviewed the diff and the checks", feedback="", verdict=Verdict.PASS
    )


def test_off_cadence_continue_outcome_skips_the_independent_judge(tmp_path: Path) -> None:
    """Round 1 (judge_every=2, not the final round) reports a bounded
    CONTINUE outcome with no fresh candidate evidence: the round finishes
    without ever invoking the judge role. Round 2 (the final round) forces
    review regardless of cadence.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _decision(), _plan())
    runner.enqueue(
        "implementer",
        ImplementerResponse(
            summary="partial batching, more work remains",
            expected_behavior="higher steady-state throughput once complete",
            hypothesis_outcome=HypothesisOutcome.CONTINUE,
            next_step="finish batching the decode path",
            evidence="ran the local checks",
        ),
        ImplementerResponse(
            summary="finished batching",
            expected_behavior="higher steady-state throughput",
            hypothesis_outcome=HypothesisOutcome.NOMINATED,
            evidence="ran the local checks",
        ),
    )
    runner.enqueue("judge", _judge())

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_agent_loop(
        tmp_path, runner, MultiAgentOrchestrator, descriptor, exp_name="sparse-review"
    )

    assert run.result is True
    assert len(runner.calls_for("implementer")) == 2
    assert len(runner.calls_for("judge")) == 1  # only round 2's forced review
    # Round 2 is a continuation of the same hypothesis: no second plan call.
    assert len(runner.calls_for("orchestrator")) == 2


def test_unparseable_implementer_reply_retries_without_reaching_the_judge(tmp_path: Path) -> None:
    """A parse-failure retries the same attempt (a fresh implementer turn,
    never the judge) instead of counting as a reviewed attempt.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _decision(), _plan())
    runner.enqueue_parse_failure("implementer", count=1)
    runner.enqueue(
        "implementer",
        ImplementerResponse(
            summary="batched the prefill step",
            expected_behavior="higher steady-state throughput",
            evidence="ran the local checks",
        ),
    )
    runner.enqueue("judge", _judge())

    descriptor = descriptor_from_options(_options(max_rounds=1), orchestration_id="multi-agent")
    run = run_agent_loop(
        tmp_path, runner, MultiAgentOrchestrator, descriptor, exp_name="unparseable-retry"
    )

    assert run.result is True
    assert len(runner.calls_for("implementer")) == 2  # the parse failure, then a clean retry
    assert len(runner.calls_for("judge")) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
