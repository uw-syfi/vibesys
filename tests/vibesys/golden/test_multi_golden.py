"""Golden snapshots for the multi-agent strategy: prompts, board files, events.

Drives ``MultiAgentOrchestrator.run(ctx)`` end-to-end (real workspace, git
tracking, progress board, event journal) against a scripted
:class:`FakeAgentClient`, the same integration pattern
``tests/vibesys/loops/evolve/test_evolutionary_loop.py`` uses for evolve.
No real agent CLI, sandbox, or subprocess ever runs.

Three scenarios cover the main round path without every branch:

- ``pass``: one round, no profiling, plan -> implement -> judge PASS.
  ``backend_name="stub"`` short-circuits framework gates (multi skips them
  entirely for a stub backend), keeping this scenario's snapshot free of
  gate noise.
- ``retry_then_pass``: judge FAILs attempt 1, feedback carries into attempt
  2's implementer prompt, judge PASSes attempt 2.
- ``gate``: same as ``pass`` but with a real (non-stub) backend and the
  accuracy gate's actual command execution replaced by a scripted
  pass/fail, so the board's "Framework accuracy gate" entry and the
  ``gate_started``/``gate_finished`` events are covered by a snapshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch  # test-isolation: seams scripted below

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
from vibesys.evaluators.gates import (
    AccuracyGateResult,
    GateKind,
    emit_gate_finished,
    emit_gate_started,
)
from vibesys.evaluators.metrics import MetricSpace
from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.events import GateFinishedData
from vibesys.loops.multi.orchestration import MultiAgentOrchestrator
from vibesys.profilers import ProfilerKind
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

_STRATEGY = "multi"


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


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040009 [S106]; the argument is a fixture literal, not a credential.
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

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_scripted(
        tmp_path,
        orchestration_id="multi-agent",
        descriptor=descriptor,
        orchestrator_factory=MultiAgentOrchestrator,
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

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_scripted(
        tmp_path,
        orchestration_id="multi-agent",
        descriptor=descriptor,
        orchestrator_factory=MultiAgentOrchestrator,
        runner=runner,
    )

    assert run.result is True
    _assert_prompt_calls(runner, scenario="retry_then_pass", workspace=tmp_path.parent)
    _assert_board_files(run.workspace, scenario="retry_then_pass", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "retry_then_pass", read_events(run.events_path, workspace=tmp_path.parent)
    )


def _scripted_accuracy_gate(*, passed: bool):  # noqa: ANN202  # LW-040010 [ANN202]; the helper is private to this test module and its return type is the local closure type.
    """Replace only the trusted command execution inside the accuracy gate.

    Keeps the real ``run_accuracy_gate`` call site in
    ``vibesys.orchestration.runtime`` (so ``gate_started``/``gate_finished``
    events and the issue-board write still come from production code), but
    removes the dependency on a real sandboxed subprocess.
    """

    def _run(ctx, *, process_id, timeout_seconds=None, execution_command=None, round_label=None):  # noqa: ANN001, ARG001, ANN202  # LW-040011 [ANN001, ANN202, ARG001]; this scripted double mirrors a production signature whose parameters are not annotated here. The helper is private to this test module and its return type is the local closure type. This scripted double accepts the production keyword arguments and ignores the ones it does not need.
        command = ctx.judge_accuracy_command
        emit_gate_started(GateKind.ACCURACY, command=command, round_label=round_label)
        emit_gate_finished(
            GateFinishedData(gate=GateKind.ACCURACY), passed=passed, round_label=round_label
        )
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

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    # test-isolation: no injectable gate seam yet; a gate-executor fake replaces this patch
    with patch(
        "vibesys.orchestration.gates.run_accuracy_gate",
        side_effect=_scripted_accuracy_gate(passed=True),
    ):
        run = run_scripted(
            tmp_path,
            orchestration_id="multi-agent",
            descriptor=descriptor,
            orchestrator_factory=MultiAgentOrchestrator,
            runner=runner,
        )

    assert run.result is True
    _assert_board_files(run.workspace, scenario="gate", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "gate", read_events(run.events_path, workspace=tmp_path.parent)
    )


def test_profile_scenario_golden(tmp_path: Path) -> None:
    """Pre-round decision requests a profile, so the profiler role runs."""
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

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_scripted(
        tmp_path,
        orchestration_id="multi-agent",
        descriptor=descriptor,
        orchestrator_factory=MultiAgentOrchestrator,
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


def test_rollback_scenario_golden(tmp_path: Path) -> None:
    """Round 2's plan sets ``revert_to_round=1``: the designer requests a
    hypothesis rollback, and round 2 runs against the restored round-1 tree.

    Exercises PR 942 review bug R1 (rollback-checkout failure must warn and
    retry, never abort the run).
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        PreRoundDecision(
            need_profile=False, profile_focus="", reasoning="scripted: skip profiling"
        ),
        _plan(),
        PreRoundDecision(
            need_profile=False, profile_focus="", reasoning="scripted: skip profiling"
        ),
        OrchestratorPlan(
            hypothesis_id="H-02",
            hypothesis="reverting to the round-1 baseline before decode batching",
            task="batch the decode step from the round-1 baseline",
            pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040012 [S106]; the argument is a fixture literal, not a credential.
            reasoning="scripted golden fixture: roll back then retry",
            revert_to_round=1,
        ),
    )
    runner.enqueue(
        "implementer",
        _implementer("round 1: batched the prefill step"),
        _implementer("round 2: batched the decode step after rollback"),
    )
    runner.enqueue("judge", _judge(Verdict.PASS), _judge(Verdict.PASS))

    descriptor = descriptor_from_options(_options(max_rounds=2), orchestration_id="multi-agent")
    run = run_scripted(
        tmp_path,
        orchestration_id="multi-agent",
        descriptor=descriptor,
        orchestrator_factory=MultiAgentOrchestrator,
        runner=runner,
    )

    assert run.result is True
    _assert_prompt_calls(runner, scenario="rollback", workspace=tmp_path.parent)
    _assert_board_files(run.workspace, scenario="rollback", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "rollback", read_events(run.events_path, workspace=tmp_path.parent)
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
