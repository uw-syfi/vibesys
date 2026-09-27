"""Real single-agent rollback restoration behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.loops._support import run_agent_loop

from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.single.orchestration import SingleAgentOrchestrator
from vibesys.orchestrations.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.schemas import CandidateDisposition
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.hypothesis.state import load_hypothesis_state
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project

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


def _plan(hypothesis_id: str, *, revert_to_round: int | None = None) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040147 [S106]; the argument is a fixture literal, not a credential.
        reasoning="scripted: roll back then retry",
        revert_to_round=revert_to_round,
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


def test_rollback_checkout_success_reverts_the_workspace(tmp_path: Path) -> None:
    """The ordinary (non-corrupted) path: round 2's rollback checkout
    succeeds, so ``hypothesis.revert_applied``/``revert_commit`` record it.
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _plan("H-01"), _plan("H-02", revert_to_round=1))
    runner.enqueue(
        "implementer",
        _combined("round 1: batched the prefill step"),
        _combined("round 2: batched the decode step after rollback"),
    )

    descriptor = descriptor_from_options(_options(), orchestration_id="single-agent")
    run = run_agent_loop(
        tmp_path, runner, SingleAgentOrchestrator, descriptor, exp_name="rollback-ok"
    )

    assert run.result is True
    project = Project.open(run.project_dir)
    state = load_hypothesis_state(project, run.run_id, namespace="single")
    assert state is not None
    hypothesis = state.by_id("H-02")
    assert hypothesis is not None
    assert hypothesis.revert_applied is True
    assert hypothesis.revert_commit is not None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
