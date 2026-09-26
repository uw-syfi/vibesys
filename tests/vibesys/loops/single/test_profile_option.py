"""Profile-guided attribution wiring for the single strategy.

Mirrors ``tests/vibesys/loops/multi/test_profile_option.py``: the
``profile_guided`` option runs a task-owned attribution command via
``ctx.environment.execute`` once per round
(``vibesys.loops.single.attribution.run_attribution``), scripted here
through a ``FakeComputeBackend`` subclass (real seam, not a mock) since the
command's UUID-suffixed output path defeats ``FakeSandbox.script``'s
exact-match lookup. Asserts attribution changes the plan context (active
component) and the official-eval reason the single-agent round prompt
shows, without needing the framework gates to run.
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
from vibesys.loops.single.orchestration import (
    ProfileGuidedSingleAgentOrchestrator,
    SingleAgentOrchestrator,
)
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.schemas import CandidateDisposition
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
                {"name": _COMPONENT, "cost": 40.0, "share": 0.4, "evidence": ["profile.json:12"]}
            ],
        }
    )
    return f"attribution ran\n{_BEGIN}\n{payload}\n{_END}\n"


class _AttributionBackend(FakeComputeBackend):
    """See ``tests.vibesys.loops.multi.test_profile_option``."""

    def make_sandbox(self, kind: SandboxKind, **kwargs: Any) -> FakeSandbox:  # noqa: ANN401  # LW-040144 [ANN401]; the value crosses an untyped boundary, so Any is the accurate type.
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
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # LW-040145 [S106]; the argument is a fixture literal, not a credential.
        reasoning="scripted",
    )


def _combined() -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="batched the prefill step",
        expected_behavior="higher steady-state throughput",
        self_review="reviewed the diff and the checks",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="prefill launch overhead dominates at low batch sizes",
        suggestions="batch decode requests next",
        profile_analysis="ran the local checks",
        candidate_disposition=CandidateDisposition.UNASSESSED,
    )


def _run(tmp_path: Path, *, profile_guided: ProfileGuidedInput | None) -> FakeAgentClient:
    """Run 2 rounds so round 1's base official-eval cadence isn't forced by
    ``official_due``'s final-round rule (see the multi-strategy sibling of
    this module for the full explanation).
    """
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _plan("H-01"), _plan("H-02"))
    runner.enqueue("implementer", _combined(), _combined())
    if profile_guided is None:
        orchestrator_cls, orchestration_id = SingleAgentOrchestrator, "single-agent"
    else:
        orchestrator_cls, orchestration_id = (
            ProfileGuidedSingleAgentOrchestrator,
            "profile-guided-single-agent",
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
    plain = _run(tmp_path / "plain", profile_guided=None)
    guided = _run(
        tmp_path / "guided", profile_guided=ProfileGuidedInput(command=("python", "attribute.py"))
    )

    plain_prompt = plain.calls_for("orchestrator")[0].system_prompt
    guided_prompt = guided.calls_for("orchestrator")[0].system_prompt
    assert _COMPONENT not in plain_prompt
    assert _COMPONENT in guided_prompt
    assert "Profile-guided focus" in guided_prompt


def test_profile_guided_attribution_changes_official_eval_reason(tmp_path: Path) -> None:
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
