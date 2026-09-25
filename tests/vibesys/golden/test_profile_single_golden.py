"""Golden snapshots for the profile-guided-single-agent strategy: prompts,
board files, events.

Drives ``ProfileGuidedSingleAgentOrchestrator.run(ctx)`` end-to-end (real
workspace, git tracking, progress board, event journal) against a scripted
:class:`FakeAgentClient`, the same integration pattern
``test_multi_golden.py`` uses.  No real agent CLI, sandbox, or subprocess
ever runs.

This strategy spawns only two roles, ``orchestrator`` (designer plan) and
``implementer`` (a combined implement + self-judge + profile turn via
``SingleAgentRoundResponse``); unlike multi it has no separate judge role and
no pre-round ``PreRoundDecision`` agent call. Its per-component profiling is
host-computed: ``select_hypothesis`` runs ``run_attribution`` directly (a
shelled-out profiler command parsed into ``ProfileBottleneck`` rows), not an
agent turn, so there is nothing to script through ``FakeAgentClient`` for it.
That attribution command is scripted out here (patching
``vibesys.loops.single.session.run_attribution``, the same
production call site production code uses) the same way multi's ``gate``
scenario scripts out the accuracy gate's trusted command execution, so the
snapshot stays hermetic and deterministic.

This strategy also forces an official evaluation on the final round
(``_official_reason`` returns ``"final_round"`` once ``round_number ==
max_rounds``), so with ``max_rounds=1`` every scenario's single round runs
the official-evaluation decision path; a stub backend still short-circuits
the gates themselves (``_run_gates`` returns immediately for
``backend_name == "stub"``), keeping the ``pass``/``retry_then_pass``
snapshots free of gate noise exactly like multi's.

Three scenarios cover the main round path without every branch:

- ``pass``: one round, plan -> combined turn PASS. ``backend_name="stub"``
  short-circuits the forced final-round gate.
- ``retry_then_pass``: combined turn FAILs attempt 1 with feedback, PASSes
  attempt 2.
- ``gate``: same as ``pass`` but with a real (non-stub) backend and the
  accuracy gate's actual command execution replaced by a scripted
  pass/fail, so the board's "Framework accuracy gate" entry and the
  ``gate_started``/``gate_finished`` events are covered by a snapshot. The
  benchmark gate's trusted command stays real (it is the harness's trivial
  ``python -c "print('ok')"`` echo), matching multi's ``gate`` scenario.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from tests.vibesys.golden.harness import run_scripted
from tests.vibesys.golden.helpers import (
    assert_board_snapshot,
    assert_events_snapshot,
    assert_prompt_snapshot,
    prompt_text,
    read_events,
)

from vibesys.evaluators.gates import (
    AccuracyGateResult,
    GateKind,
    emit_gate_finished,
    emit_gate_started,
)
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.single.orchestration import ProfileGuidedSingleAgentOrchestrator
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.schemas import (
    CandidateDisposition,
)
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.profile_focus.state import ProfileBottleneck
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

_STRATEGY = "profile_single"
_ORCHESTRATION_ID = "profile-guided-single-agent"


def _options(**overrides: object) -> AgentOrchestrationOptions:
    values: dict[str, object] = {
        "interface": "inprocess",
        "max_rounds": 1,
        "max_retries_per_round": 2,
        "judge_every": 1,
        "official_eval_every": 1,
        "memory_layout": "files",
        "metric_space": MetricSpace(),
        "profile_guided": ProfileGuidedInput(command=("profile-tool",)),
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


def _combined(
    verdict: Verdict, feedback: str = "", summary: str = "batched the prefill step"
) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary=summary,
        expected_behavior="higher steady-state throughput",
        self_review="reviewed the diff and the checks",
        feedback=feedback,
        verdict=verdict,
        bottlenecks="prefill dominates end-to-end latency",
        suggestions="fuse the prefill launches further",
        profile_analysis="prefill accounts for the majority of measured cost",
        candidate_disposition=CandidateDisposition.UNASSESSED,
    )


def _attribution() -> tuple[ProfileBottleneck, ...]:
    return (
        ProfileBottleneck(
            name="prefill", cost=0.72, share=0.6, evidence=["scripted golden attribution"]
        ),
        ProfileBottleneck(
            name="decode", cost=0.48, share=0.4, evidence=["scripted golden attribution"]
        ),
    )


def _patch_attribution():  # noqa: ANN202  # tracked: #288
    """Replace only the host-computed profiler command this strategy runs.

    Keeps the real ``select_hypothesis`` call site in
    ``vibesys.loops.single.session`` (so the round-1 hypothesis
    start, plan prompt, and profile-guidance context still come from
    production code), but removes the dependency on a real shelled-out
    profiler command.
    """
    return patch(
        "vibesys.loops.single.session.run_attribution",
        side_effect=lambda *_args, **_kwargs: _attribution(),
    )


def test_pass_scenario_golden(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _plan())
    runner.enqueue("implementer", _combined(Verdict.PASS))

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    with _patch_attribution():
        run = run_scripted(
            tmp_path,
            orchestration_id=_ORCHESTRATION_ID,
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedSingleAgentOrchestrator,
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
    runner.enqueue("orchestrator", _plan())
    runner.enqueue(
        "implementer",
        _combined(
            Verdict.FAIL,
            feedback="batching only covers the prefill path, not decode",
            summary="first attempt: partial batching",
        ),
        _combined(Verdict.PASS, summary="second attempt: full batching after self-review"),
    )

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    with _patch_attribution():
        run = run_scripted(
            tmp_path,
            orchestration_id=_ORCHESTRATION_ID,
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedSingleAgentOrchestrator,
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
    removes the dependency on a real sandboxed subprocess. Copied verbatim
    from ``test_multi_golden.py``.
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
    runner.enqueue("orchestrator", _plan())
    runner.enqueue("implementer", _combined(Verdict.PASS))

    descriptor = descriptor_from_options(_options(), orchestration_id=_ORCHESTRATION_ID)
    with (
        _patch_attribution(),
        patch(
            "vibesys.orchestration.gates.run_accuracy_gate",
            side_effect=_scripted_accuracy_gate(passed=True),
        ),
    ):
        run = run_scripted(
            tmp_path,
            orchestration_id=_ORCHESTRATION_ID,
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedSingleAgentOrchestrator,
            runner=runner,
        )

    assert run.result is True
    _assert_board_files(run.workspace, scenario="gate", workspace=tmp_path.parent)
    assert_events_snapshot(
        _STRATEGY, "gate", read_events(run.events_path, workspace=tmp_path.parent)
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
