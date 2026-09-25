"""Judge-approved local validation recipes, end to end through the real
``MultiAgentOrchestrator`` (``vibesys.loops.multi.session.MultiSession._validate_local``).

An implementer that names ``validation_recipe_artifact`` asks the framework
to run one or more local, candidate-authored commands (audited by the
recipe schema, executed by the host, never by the agent) before a PASS
attempt is accepted. This module writes the artifact file the real agent
would have written (an ``on_invoke`` hook writing to the real, on-disk
workspace -- a real seam, not a mocked filesystem), and scripts the
recipe's *command* execution through ``FakeComputeBackend``'s sandbox (the
same real seam ``test_profile_option.py`` uses for attribution), covering
both the pass path (candidate accepted) and the fail path (one more retry,
carrying the framework's validation feedback).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.vibesys.loops._support import run_agent_loop

from vibesys.backends.fake import FakeComputeBackend
from vibesys.evaluators.metrics import MetricSpace
from vibesys.evaluators.validation_recipe import ValidationRecipe, ValidationRecipeArtifact
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.multi.orchestration import MultiAgentOrchestrator
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.pre_round import PreRoundDecision
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient
from vs_sandbox.execution import SandboxExecutionResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.backends.base import SandboxKind
    from vibesys.constants import ComputeBackend
    from vs_agent.api.testing import FakeInvocation
    from vs_sandbox.fake_sandbox import FakeSandbox

_RECIPE_NAME = "focused-tests"
_ARTIFACT_NAME = "recipes.json"


def _write_recipe_artifact(workspace: Path, *, command: str) -> None:
    recipe = ValidationRecipe(
        name=_RECIPE_NAME,
        command=command,
        input_paths=["OBJECTIVE.md"],
        purpose="Check the changed server behavior locally.",
    )
    (workspace / _ARTIFACT_NAME).write_text(
        ValidationRecipeArtifact(recipes=[recipe]).model_dump_json()
    )


class _RecipeBackend(FakeComputeBackend):
    """Return the scripted recipe-command outcome for any sandbox command
    (the local-validation recipe's exact command string isn't threaded
    through this test's setup, so this matches the ``_AttributionBackend``
    pattern of scripting every call rather than one exact string)."""

    def __init__(self, *, exit_code: int, output: str) -> None:
        super().__init__()
        self._exit_code = exit_code
        self._output = output

    def make_sandbox(self, kind: SandboxKind, **kwargs: Any) -> FakeSandbox:  # noqa: ANN401  # tracked: #288
        sandbox = cast("FakeSandbox", super().make_sandbox(kind, **kwargs))
        sandbox.default_result = SandboxExecutionResult(
            output=self._output, exit_code=self._exit_code, stdout=self._output, stderr=""
        )
        return sandbox


def _backend_factory(
    *, exit_code: int, output: str
) -> Callable[..., _RecipeBackend]:  # tracked: #288
    def factory(backend: ComputeBackend, **_kwargs: object) -> _RecipeBackend:
        del backend
        return _RecipeBackend(exit_code=exit_code, output=output)

    return factory


def _options() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions.model_validate(
        {
            "interface": "inprocess",
            "max_rounds": 1,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 1,
            "memory_layout": "files",
            "metric_space": MetricSpace(),
        }
    )


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


def _implementer(
    summary: str = "batched the prefill step",
    *,
    validation_recipe_artifact: str | None = _ARTIFACT_NAME,
) -> ImplementerResponse:
    return ImplementerResponse(
        summary=summary,
        expected_behavior="higher steady-state throughput",
        evidence="ran the local checks",
        validation_recipe_artifact=validation_recipe_artifact,
    )


def _judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="reviewed the diff and the checks", feedback="", verdict=Verdict.PASS
    )


def _write_recipe_on_implementer(fake: FakeAgentClient, *, command: str) -> None:
    def _hook(call: FakeInvocation) -> None:
        if call.kind == "implementer":
            _write_recipe_artifact(call.workspace, command=command)

    fake.on_invoke(_hook)


def test_local_validation_pass_accepts_the_candidate(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _decision(), _plan())
    runner.enqueue("implementer", _implementer())
    runner.enqueue("judge", _judge())
    _write_recipe_on_implementer(runner, command="python -m pytest tests/test_server.py")

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_agent_loop(
        tmp_path,
        runner,
        MultiAgentOrchestrator,
        descriptor,
        exp_name="local-validation-pass",
        backend_factory=_backend_factory(exit_code=0, output="3 passed"),
    )

    assert run.result is True
    assert len(runner.calls_for("implementer")) == 1
    artifact_dir = run.project_dir / "progress-artifacts" / "validation"
    assert artifact_dir.exists(), "framework validation ledger must be written on a run"
    results = list(artifact_dir.glob("round-*.json"))
    assert results, "expected at least one validation result artifact"
    payload = json.loads(results[0].read_text())
    assert payload["results"][0]["passed"] is True
    assert payload["results"][0]["recipe"]["name"] == _RECIPE_NAME


def test_local_validation_failure_retries_with_feedback(tmp_path: Path) -> None:
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _decision(), _plan())
    runner.enqueue(
        "implementer",
        _implementer("attempt 1"),
        _implementer("attempt 2, no recipe", validation_recipe_artifact=None),
    )
    runner.enqueue("judge", _judge(), _judge())
    _write_recipe_on_implementer(runner, command="python -m pytest tests/test_server.py")

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    run = run_agent_loop(
        tmp_path,
        runner,
        MultiAgentOrchestrator,
        descriptor,
        exp_name="local-validation-fail",
        backend_factory=_backend_factory(exit_code=1, output="1 failed, 2 passed"),
    )

    assert run.result is True
    # Attempt 1 fails local validation and retries; attempt 2's implementer
    # response carries no validation_recipe_artifact, so it isn't re-checked.
    assert len(runner.calls_for("implementer")) == 2
    implementer_calls = runner.calls_for("implementer")
    assert "Framework local validation failed" in implementer_calls[1].system_prompt


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
