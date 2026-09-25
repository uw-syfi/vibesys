"""Golden snapshots for the profile-guided-multi-agent strategy: prompts,
board files, events.

Drives ``ProfileGuidedMultiAgentOrchestrator.run(ctx)`` end-to-end (real
workspace, git tracking, progress board, event journal) against a scripted
:class:`FakeAgentClient`, the same integration pattern
``tests/vibesys/golden/test_multi_golden.py`` uses for plain multi.
No real agent CLI, sandbox, or subprocess ever runs.

profile_multi shares its round shape (designer/implementer/judge, plus an
optional profiler) with plain multi, so these scenarios mirror the multi
golden suite:

- ``pass``: one round, no pre-round profiling, plan -> implement -> judge
  PASS. ``backend_name="stub"`` short-circuits framework gates (profile_multi
  skips them entirely for a stub backend, same as multi), keeping this
  scenario's snapshot free of gate noise.
- ``retry_then_pass``: judge FAILs attempt 1, feedback carries into attempt
  2's implementer prompt, judge PASSes attempt 2.
- ``gate``: same as ``pass`` but with a real (non-stub) backend and the
  accuracy gate's actual command execution replaced by a scripted
  pass/fail, so the board's "Framework accuracy gate" entry and the
  ``gate_started``/``gate_finished`` events are covered by a snapshot.

Unlike plain multi, profile_multi requires an ``options.profile_guided``
config and always runs its component-attribution step once per round
(``vibesys.loops.profile_multi.session.run_attribution``), independent of
the pre-round profiler decision and of backend. That step shells out to the
configured attribution command via ``ctx.environment.execute``, so every
scenario here replaces it with a scripted empty-attribution result, the same
seam ``tests/vibesys/loops/profile_multi/test_session.py`` uses. A dedicated
scenario that actually invokes the profiler *role* is not reachable through
this shared harness: ``run_scripted`` hardcodes
``profiler_kind=ProfilerKind.NONE`` on its ``RunRequest``, and
``ProfileMultiTurns.profile`` returns ``None`` without ever invoking the
profiler agent whenever the resolved profiler kind is ``NONE`` -- regardless
of ``PreRoundDecision.need_profile``. See the report for this gap; it is not
patched here per the task's harness/helpers constraint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from tests.vibesys.golden.harness import run_scripted
from tests.vibesys.golden.helpers import (
    assert_board_snapshot,
    assert_events_snapshot,
    assert_prompt_snapshot,
    prompt_text,
    read_events,
)

from vibesys.agent_run.options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.agent_run.state import ProfileBottleneck
from vibesys.evaluators.gates import (
    AccuracyGateResult,
    GateKind,
    emit_gate_finished,
    emit_gate_started,
)
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.profile_multi.orchestration import ProfileGuidedMultiAgentOrchestrator
from vibesys.profilers import ProfilerKind
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.roles.profiler import ProfilerSummary
from vibesys.schemas import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

_STRATEGY = "profile_multi"
_ORCHESTRATION_ID = "profile-guided-multi-agent"


def _options(**overrides: object) -> AgentOrchestrationOptions:
    values: dict[str, object] = {
        "interface": "inprocess",
        "max_rounds": 1,
        "max_retries_per_round": 2,
        "judge_every": 1,
        "official_eval_every": 1,
        "memory_layout": "files",
        "metric_space": MetricSpace(),
        "profile_guided": ProfileGuidedInput(command=("profile",)),
    }
    values.update(overrides)
    return AgentOrchestrationOptions.model_validate(values)


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # tracked: #288
        reasoning="scripted golden fixture",
    )


def _implementer(summary: str = "batched the prefill step") -> ImplementerResponse:
    return ImplementerResponse(
        summary=summary,
        expected_behavior="higher steady-state throughput",
        evidence="ran the local checks",
    )


def _judge(verdict: Verdict, feedback: str = "") -> JudgeResponse:
    return JudgeResponse(
        analysis="reviewed the diff and the checks", feedback=feedback, verdict=verdict
    )


def _no_attribution() -> AsyncMock:
    """Replace the per-round component-attribution shell-out with an empty result.

    Keeps ``run_attribution``'s call site in
    ``vibesys.loops.profile_multi.session`` (production round control), but
    removes the dependency on a real profiler command running in the
    sandbox, the same seam
    ``tests/vibesys/loops/profile_multi/test_session.py`` uses.
    """
    return AsyncMock(return_value=())


def test_pass_scenario_golden(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        PreRoundDecision(
            need_profile=False, profile_focus="", reasoning="scripted: skip profiling"
        ),
        _plan(),
    )
    runner.enqueue("implementer", _implementer())
    runner.enqueue("judge", _judge(Verdict.PASS))

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    with patch("vibesys.loops.profile_multi.session.run_attribution", new=_no_attribution()):
        run = run_scripted(
            tmp_path,
            orchestration_id=_ORCHESTRATION_ID,
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedMultiAgentOrchestrator,
            runner=runner,
        )

    assert run.result is True
    _assert_prompt_calls(runner, scenario="pass", workspace=tmp_path.parent)
    _assert_board_files(run.workspace, scenario="pass", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "pass", read_events(run.events_path, workspace=tmp_path.parent)
    )


def test_retry_then_pass_scenario_golden(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        PreRoundDecision(
            need_profile=False, profile_focus="", reasoning="scripted: skip profiling"
        ),
        _plan(),
    )
    runner.enqueue(
        "implementer",
        _implementer("first attempt: partial batching"),
        _implementer("second attempt: full batching after judge feedback"),
    )
    runner.enqueue(
        "judge",
        _judge(Verdict.FAIL, feedback="batching only covers the prefill path, not decode"),
        _judge(Verdict.PASS),
    )

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    with patch("vibesys.loops.profile_multi.session.run_attribution", new=_no_attribution()):
        run = run_scripted(
            tmp_path,
            orchestration_id=_ORCHESTRATION_ID,
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedMultiAgentOrchestrator,
            runner=runner,
        )

    assert run.result is True
    _assert_prompt_calls(runner, scenario="retry_then_pass", workspace=tmp_path.parent)
    _assert_board_files(run.workspace, scenario="retry_then_pass", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "retry_then_pass", read_events(run.events_path, workspace=tmp_path.parent)
    )


def _scripted_accuracy_gate(*, passed: bool):  # noqa: ANN202  # tracked: #288
    """Replace only the trusted command execution inside the accuracy gate.

    Keeps the real ``run_accuracy_gate`` call site in
    ``vibesys.orchestration.runtime`` (so ``gate_started``/``gate_finished``
    events and the issue-board write still come from production code), but
    removes the dependency on a real sandboxed subprocess.
    """

    def _run(ctx, *, process_id, timeout_seconds=None, execution_command=None, round_label=None):  # noqa: ANN001, ARG001, ANN202  # tracked: #288
        command = ctx.judge_accuracy_command
        emit_gate_started(GateKind.ACCURACY, command=command, round_label=round_label)
        emit_gate_finished(GateKind.ACCURACY, passed=passed, round_label=round_label)
        return AccuracyGateResult(
            command=command,
            passed=passed,
            output="scripted accuracy gate output",
            feedback=None if passed else "scripted accuracy gate failure",
            executed=True,
        )

    return _run


def test_gate_scenario_golden(tmp_path: Path) -> None:
    runner = FakeAgentClient()  # real (non-stub) backend: gates execute
    runner.enqueue(
        "orchestrator",
        PreRoundDecision(
            need_profile=False, profile_focus="", reasoning="scripted: skip profiling"
        ),
        _plan(),
    )
    runner.enqueue("implementer", _implementer())
    runner.enqueue("judge", _judge(Verdict.PASS))

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    with (
        patch("vibesys.loops.profile_multi.session.run_attribution", new=_no_attribution()),
        patch(
            "vibesys.orchestration.gates.run_accuracy_gate",
            side_effect=_scripted_accuracy_gate(passed=True),
        ),
    ):
        run = run_scripted(
            tmp_path,
            orchestration_id=_ORCHESTRATION_ID,
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedMultiAgentOrchestrator,
            runner=runner,
        )

    assert run.result is True
    _assert_board_files(run.workspace, scenario="gate", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "gate", read_events(run.events_path, workspace=tmp_path.parent)
    )


def test_profile_scenario_golden(tmp_path: Path) -> None:
    """Attribution reports bottlenecks and the pre-round decision requests a
    profile, so the focus guidance and the profiler role both appear."""
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        PreRoundDecision(
            need_profile=True, profile_focus="prefill kernels", reasoning="scripted: profile"
        ),
        _plan(),
    )
    runner.enqueue(
        "profiler",
        ProfilerSummary(
            analysis="prefill dominates step time",
            bottlenecks="1. per-request prefill launch: 40% of step time",
            suggestions="batch prefill requests",
            perf_metric=1000.0,
            perf_unit="tok/s",
        ),
    )
    runner.enqueue("implementer", _implementer())
    runner.enqueue("judge", _judge(Verdict.PASS))
    attribution = AsyncMock(
        return_value=(
            ProfileBottleneck(name="prefill", cost=0.72, share=0.6, evidence=["scripted"]),
            ProfileBottleneck(name="decode", cost=0.48, share=0.4, evidence=["scripted"]),
        )
    )

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    with patch("vibesys.loops.profile_multi.session.run_attribution", new=attribution):
        run = run_scripted(
            tmp_path,
            orchestration_id=_ORCHESTRATION_ID,
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedMultiAgentOrchestrator,
            runner=runner,
            profiler_kind=ProfilerKind.TORCH,
        )

    assert run.result is True
    assert runner.calls_for("profiler"), "profile scenario must invoke the profiler role"
    _assert_prompt_calls(runner, scenario="profile", workspace=tmp_path.parent)
    _assert_board_files(run.workspace, scenario="profile", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "profile", read_events(run.events_path, workspace=tmp_path.parent)
    )


def _assert_prompt_calls(runner: FakeAgentClient, *, scenario: str, workspace: Path) -> None:
    for call in runner.calls:
        role = f"{call.kind}-{call.round_label}"
        assert_prompt_snapshot(
            _STRATEGY,
            role,
            scenario,
            prompt_text(call.system_prompt, call.user_prompt),
            workspace=workspace,
        )


_BOARD_FILES = ("progress.md", "roadmap.md", "pareto-frontier.md")


def _assert_board_files(project_dir: Path, *, scenario: str, workspace: Path) -> None:
    for relative in _BOARD_FILES:
        path = project_dir / relative
        if not path.exists():
            continue
        assert_board_snapshot(_STRATEGY, scenario, relative, path.read_text(), workspace=workspace)
    plans_dir = project_dir / "progress-artifacts" / "plans"
    if plans_dir.exists():
        for plan_file in sorted(plans_dir.glob("*.json")):
            relative = f"progress-artifacts/plans/{plan_file.name}"
            assert_board_snapshot(
                _STRATEGY, scenario, relative, plan_file.read_text(), workspace=workspace
            )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
