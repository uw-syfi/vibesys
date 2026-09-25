"""Resume-after-crash for the multi-agent strategy, driven through the real
``MultiAgentOrchestrator`` (``tests.vibesys.loops._support``, which uses
``run_orchestration``'s ``agent_client_factory``/``backend_factory`` seams
with ``FakeAgentClient``/``FakeComputeBackend``, never
``unittest.mock.patch`` on a collaborator).

The clean pass / retry / gate / profile / rollback scenarios are
golden-snapshotted in ``tests/vibesys/golden/test_multi_golden.py``. This
module covers what those don't: a crash mid-round resumes without repeating
already-committed turns, and reaches the same final hypothesis state an
uninterrupted run with the same script would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.loops._support import run_agent_loop, run_agent_loop_expect_crash

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

    from tests.vibesys.loops._support import AgentRun

    from vibesys.search.hypothesis.state import HypothesisState
    from vs_project.api import OrchestrationDescriptor


class _CrashError(Exception):
    """Stand-in for a killed process, raised mid-turn."""


def _options(**overrides: object) -> AgentOrchestrationOptions:
    values: dict[str, object] = {
        "interface": "inprocess",
        "max_rounds": 1,
        "max_retries_per_round": 2,
        "judge_every": 1,
        "official_eval_every": 1,
        "memory_layout": "files",
        "metric_space": MetricSpace(),
    }
    values.update(overrides)
    return AgentOrchestrationOptions.model_validate(values)


def _descriptor(**overrides: object) -> OrchestrationDescriptor:
    return descriptor_from_options(_options(**overrides), orchestration_id="multi-agent")


def _decision() -> PreRoundDecision:
    return PreRoundDecision(need_profile=False, profile_focus="", reasoning="scripted")


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106
        reasoning="scripted",
    )


def _implementer() -> ImplementerResponse:
    return ImplementerResponse(
        summary="batched the prefill step",
        expected_behavior="higher steady-state throughput",
        evidence="ran the local checks",
    )


def _judge(verdict: Verdict = Verdict.PASS) -> JudgeResponse:
    return JudgeResponse(analysis="reviewed the diff and the checks", feedback="", verdict=verdict)


def _final_state(run: AgentRun) -> HypothesisState | None:
    project = Project.open(run.project_dir)
    return load_hypothesis_state(project, run.run_id, namespace="multi")


def test_resumes_at_next_attempt_without_repeating_orchestrator(tmp_path: Path) -> None:
    """A crash mid-judge-turn resumes without re-running the orchestrator's
    planning turn: the committed implementer attempt is the crash-safety
    boundary (its paid-work marker), so resume starts a fresh attempt rather
    than replaying the judge for the crashed one.
    """
    crasher = FakeAgentClient(backend_name="stub")
    crasher.enqueue("orchestrator", _decision(), _plan())
    crasher.enqueue("implementer", _implementer())
    crasher.fail("judge", _CrashError())
    crashed = run_agent_loop_expect_crash(
        tmp_path,
        crasher,
        MultiAgentOrchestrator,
        _descriptor(),
        _CrashError,
        exp_name="resume-multi",
    )

    resumer = FakeAgentClient(backend_name="stub")
    resumer.enqueue("implementer", _implementer())
    resumer.enqueue("judge", _judge())
    resumed = run_agent_loop(
        tmp_path, resumer, MultiAgentOrchestrator, _descriptor(), resume_from=crashed
    )

    assert resumed.result is True
    assert resumer.calls_for("orchestrator") == []
    assert len(resumer.calls_for("implementer")) == 1
    assert len(resumer.calls_for("judge")) == 1
    state = _final_state(resumed)
    assert state is not None
    assert len(state.rounds) == 1


def test_resumes_at_implementer_without_repeating_plan(tmp_path: Path) -> None:
    """A crash after the plan commits, mid-implementer-turn, resumes without
    re-running the orchestrator's planning turn.
    """
    crasher = FakeAgentClient(backend_name="stub")
    crasher.enqueue("orchestrator", _decision(), _plan())
    crasher.fail("implementer", _CrashError())
    crashed = run_agent_loop_expect_crash(
        tmp_path,
        crasher,
        MultiAgentOrchestrator,
        _descriptor(),
        _CrashError,
        exp_name="resume-multi-2",
    )

    resumer = FakeAgentClient(backend_name="stub")
    resumer.enqueue("implementer", _implementer())
    resumer.enqueue("judge", _judge())
    resumed = run_agent_loop(
        tmp_path, resumer, MultiAgentOrchestrator, _descriptor(), resume_from=crashed
    )

    assert resumed.result is True
    assert resumer.calls_for("orchestrator") == []
    assert len(resumer.calls_for("implementer")) == 1
    state = _final_state(resumed)
    assert state is not None
    assert len(state.rounds) == 1


@given(crash_role=st.sampled_from(["implementer", "judge"]))
@settings(max_examples=2, deadline=None)
def test_resume_reaches_same_final_state_as_uninterrupted_run(
    crash_role: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Whichever turn a run crashes mid-way through, resuming it against the
    same script reaches the same final round count and verdict an
    uninterrupted run of that script would.
    """
    baseline_dir = tmp_path_factory.mktemp("baseline")
    baseline_client = FakeAgentClient(backend_name="stub")
    baseline_client.enqueue("orchestrator", _decision(), _plan())
    baseline_client.enqueue("implementer", _implementer())
    baseline_client.enqueue("judge", _judge())
    baseline = run_agent_loop(
        baseline_dir, baseline_client, MultiAgentOrchestrator, _descriptor(), exp_name="baseline"
    )
    baseline_state = _final_state(baseline)
    assert baseline_state is not None

    crash_dir = tmp_path_factory.mktemp(f"crash-{crash_role}")
    crasher = FakeAgentClient(backend_name="stub")
    crasher.enqueue("orchestrator", _decision(), _plan())
    if crash_role == "implementer":
        crasher.fail("implementer", _CrashError())
    else:
        crasher.enqueue("implementer", _implementer())
        crasher.fail("judge", _CrashError())
    crashed = run_agent_loop_expect_crash(
        crash_dir, crasher, MultiAgentOrchestrator, _descriptor(), _CrashError, exp_name="crashed"
    )

    resumer = FakeAgentClient(backend_name="stub")
    resumer.enqueue("implementer", _implementer())
    resumer.enqueue("judge", _judge())
    resumed = run_agent_loop(
        crash_dir, resumer, MultiAgentOrchestrator, _descriptor(), resume_from=crashed
    )

    resumed_state = _final_state(resumed)
    assert resumed_state is not None
    assert resumed.result == baseline.result
    assert len(resumed_state.rounds) == len(baseline_state.rounds)
    assert resumed_state.rounds[-1].passed == baseline_state.rounds[-1].passed
    assert resumed_state.rounds[-1].judge_verdict == baseline_state.rounds[-1].judge_verdict


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
