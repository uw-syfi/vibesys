"""Rollback-checkout failure warns and retries instead of aborting the run.

``tests/vibesys/golden/test_multi_golden.py``'s ``rollback`` scenario covers
the ordinary path (a checkout that succeeds). PR 942 review bug R1 is the
failure path: a rollback-style restore is now hosted in
``WorkspaceHandle.restore_or_warn`` (``vibesys.orchestration.workspaces``),
which converts a failed checkout into a framework warning and a ``False``
return instead of letting ``WorkspaceRestoreError`` propagate. This module
forces that real failure through the actual seam it detects (a real ``git
restore --source=<sha>`` command failing), not by patching
``restore_or_warn``/``checkout_tree``: an ``on_invoke`` hook deletes the
round-1 commit's loose git object from the workspace's real ``.git/objects``
directory right as round 2's plan (naming ``revert_to_round=1``) is
returned, so the rollback that follows genuinely cannot check it out.
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
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.hypothesis.state import load_hypothesis_state
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api.testing import FakeInvocation


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


def _decision() -> PreRoundDecision:
    return PreRoundDecision(need_profile=False, profile_focus="", reasoning="scripted")


def _plan(hypothesis_id: str, *, revert_to_round: int | None = None) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040141 [S106]; the argument is a fixture literal, not a credential.
        reasoning="scripted: roll back then retry",
        revert_to_round=revert_to_round,
    )


def _implementer(summary: str) -> ImplementerResponse:
    return ImplementerResponse(
        summary=summary, expected_behavior="higher steady-state throughput", evidence="ran checks"
    )


def _judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="reviewed the diff and the checks", feedback="", verdict=Verdict.PASS
    )


def _corrupt_round_1_commit(fake: FakeAgentClient) -> None:
    """Delete round 1's commit object from the real on-disk git repository
    once round 2's plan (naming ``revert_to_round=1``) has been recorded,
    so the rollback that follows genuinely cannot check that commit out.
    """

    def _hook(call: FakeInvocation) -> None:
        if call.kind != "orchestrator" or len(fake.calls_for("orchestrator")) != 4:  # tracked: #288
            return
        project = Project.open(call.workspace)
        run = project.state.resolve_run()
        state = load_hypothesis_state(project, run.run_id, namespace="multi")
        assert state is not None
        assert len(state.rounds) == 1
        sha = state.rounds[0].commit
        assert sha is not None
        object_path = call.workspace / ".git" / "objects" / sha[:2] / sha[2:]
        assert object_path.is_file(), f"expected a loose git object at {object_path}"
        object_path.unlink()

    fake.on_invoke(_hook)


def test_rollback_checkout_failure_warns_and_continues_without_aborting(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        _decision(),
        _plan("H-01"),
        _decision(),
        _plan("H-02", revert_to_round=1),
    )
    runner.enqueue(
        "implementer",
        _implementer("round 1: batched the prefill step"),
        _implementer("round 2: batched the decode step after rollback"),
    )
    runner.enqueue("judge", _judge(), _judge())
    _corrupt_round_1_commit(runner)

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_agent_loop(
        tmp_path, runner, MultiAgentOrchestrator, descriptor, exp_name="rollback-failure"
    )

    # The run completes rather than aborting with WorkspaceRestoreError: the
    # host caught the checkout failure, warned, and round 2 proceeded on the
    # current (un-reverted) tree instead.
    assert run.result is True
    assert len(runner.calls_for("implementer")) == 2  # round 2 still ran its attempt
    project = Project.open(run.project_dir)
    state = load_hypothesis_state(project, run.run_id, namespace="multi")
    assert state is not None
    assert len(state.rounds) == 2
    hypothesis = state.by_id("H-02")
    assert hypothesis is not None
    # The rollback never actually applied: nothing to revert_applied/commit.
    assert hypothesis.revert_applied is False
    assert hypothesis.revert_commit is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
