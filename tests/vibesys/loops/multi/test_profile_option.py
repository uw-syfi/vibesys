"""Profile-guided attribution wiring for the multi strategy.

The ``profile_guided`` option is a different knob from ``profiler_kind``
(exercised by the ``profile`` golden scenario): it runs a task-owned
attribution *command* via ``ctx.environment.execute`` once per round
(``vibesys.loops.multi.attribution.run_attribution``), independent of the
pre-round profiler decision. This module scripts that command's real seam
(``FakeComputeBackend``'s sandbox, subclassed to return a framed
result-protocol-v1 payload for the attribution command's unpredictable,
UUID-suffixed invocation, since ``FakeSandbox.script`` matches by exact
command string) and asserts the two things the design brief calls out:
attribution output changes the plan context (the designer's prompt shows
the selected active component) and the official-eval reason surfaced to
the implementer/judge (``official_evaluation_reason``), without needing the
gates to actually run.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.vibesys.loops._support import run_agent_loop

from vibesys.backends.fake import FakeComputeBackend
from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.multi.orchestration import (
    MultiAgentOrchestrator,
    ProfileGuidedMultiAgentOrchestrator,
)
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient
from vs_sandbox.execution import SandboxExecutionResult

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.backends.base import SandboxKind
    from vibesys.constants import ComputeBackend
    from vs_sandbox.fake_sandbox import FakeSandbox

_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_END = "__VIBESYS_ATTRIBUTION_END__"
_COMPONENT = "prefill_launch_overhead"


def _attribution_output() -> str:
    payload = json.dumps(
        {
            "version": 1,
            "cost_unit": "ms",
            "components": [
                {
                    "name": _COMPONENT,
                    "cost": 40.0,
                    "share": 0.4,
                    "evidence": ["profile.json:12"],
                }
            ],
        }
    )
    return f"attribution ran\n{_BEGIN}\n{payload}\n{_END}\n"


class _AttributionBackend(FakeComputeBackend):
    """``FakeComputeBackend`` whose sandboxes return a scripted attribution
    payload for any command, since the real attribution command embeds a
    random UUID output path that ``FakeSandbox.script``'s exact-match
    lookup cannot predict.
    """

    def make_sandbox(self, kind: SandboxKind, **kwargs: Any) -> FakeSandbox:  # noqa: ANN401  # tracked: #288
        sandbox = cast("FakeSandbox", super().make_sandbox(kind, **kwargs))
        sandbox.default_result = SandboxExecutionResult(
            output=_attribution_output(),
            exit_code=0,
            stdout=_attribution_output(),
            stderr="",
        )
        return sandbox


def _attribution_backend_factory(backend: ComputeBackend, **_kwargs: object) -> _AttributionBackend:
    del backend
    return _AttributionBackend()


def _options(*, profile_guided: ProfileGuidedInput | None) -> AgentOrchestrationOptions:
    values: dict[str, object] = {
        "interface": "inprocess",
        "max_rounds": 2,
        "max_retries_per_round": 2,
        "judge_every": 1,
        "official_eval_every": 100,
        "memory_layout": "files",
        "metric_space": MetricSpace(),
        "profile_guided": profile_guided,
    }
    return AgentOrchestrationOptions.model_validate(values)


def _plan(hypothesis_id: str = "H-01") -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
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


def _decision() -> PreRoundDecision:
    return PreRoundDecision(need_profile=False, profile_focus="", reasoning="scripted")


def _judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="reviewed the diff and the checks", feedback="", verdict=Verdict.PASS
    )


def _run(tmp_path: Path, *, profile_guided: ProfileGuidedInput | None) -> FakeAgentClient:
    """Run 2 rounds (``max_rounds=2``) so round 1's base official-eval cadence
    isn't forced by ``official_due``'s final-round rule; round 2's script is
    scripted identically and unused by this module's assertions.

    ``profile_guided=None`` drives the plain ``multi-agent`` orchestrator
    (which forbids the option); a value drives the
    ``profile-guided-multi-agent`` preset (which requires it).
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _decision(), _plan("H-01"), _decision(), _plan("H-02"))
    runner.enqueue("implementer", _implementer(), _implementer())
    runner.enqueue("judge", _judge(), _judge())
    if profile_guided is None:
        orchestrator_cls, orchestration_id = MultiAgentOrchestrator, "multi-agent"
    else:
        orchestrator_cls, orchestration_id = (
            ProfileGuidedMultiAgentOrchestrator,
            "profile-guided-multi-agent",
        )
    descriptor = descriptor_from_options(
        _options(profile_guided=profile_guided), orchestration_id=orchestration_id
    )
    run_agent_loop(
        tmp_path,
        runner,
        orchestrator_cls,
        descriptor,
        exp_name="profile-option",
        backend_factory=_attribution_backend_factory,
    )
    return runner


def test_profile_guided_attribution_changes_plan_context(tmp_path: Path) -> None:
    """The designer's round-1 plan prompt shows the attributed active
    component only when ``profile_guided`` is on.
    """
    plain = _run(tmp_path / "plain", profile_guided=None)
    guided = _run(
        tmp_path / "guided", profile_guided=ProfileGuidedInput(command=("python", "attribute.py"))
    )

    plain_prompt = plain.calls_for("orchestrator")[1].system_prompt
    guided_prompt = guided.calls_for("orchestrator")[1].system_prompt
    assert _COMPONENT not in plain_prompt
    assert _COMPONENT in guided_prompt
    assert "Profile-guided focus" in guided_prompt


def test_profile_guided_attribution_changes_official_eval_reason(tmp_path: Path) -> None:
    """With ``official_eval_every`` set high enough that round 1's base
    cadence defers official evaluation, profile guidance still forces one
    by naming the active component: the implementer/judge prompts show
    ``official_evaluation_reason`` only in the profile-guided run.
    """
    plain = _run(tmp_path / "plain", profile_guided=None)
    guided = _run(
        tmp_path / "guided", profile_guided=ProfileGuidedInput(command=("python", "attribute.py"))
    )

    plain_prompt = plain.calls_for("implementer")[0].system_prompt
    guided_prompt = guided.calls_for("implementer")[0].system_prompt
    assert "profile-guided component measurement" not in plain_prompt
    assert "profile-guided component measurement" in guided_prompt


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
