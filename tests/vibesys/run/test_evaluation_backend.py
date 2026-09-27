"""Public composition behavior for trusted evaluation reuse."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vs_evaluation.api import (
    AwaitCall,
    ContentDigest,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationLifecycleEvent,
    EvaluationState,
    EvidenceKind,
    SubmitCall,
    SubmittedReply,
)
from vs_project.api import StateNamespace
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentToolBindingContext,
    BenchmarkEvaluation,
    BenchmarkObjective,
    MetricDirection,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path


def _digest(value: str) -> ContentDigest:
    return ContentDigest.sha256(value.encode())


def _identity(**changes: str) -> SemanticEvaluationIdentity:
    values = {
        "evaluator": "evaluator",
        "workload": "workload",
        "environment": "environment",
        **changes,
    }
    return SemanticEvaluationIdentity(
        evaluator=_digest(values["evaluator"]),
        workload=_digest(values["workload"]),
        environment=_digest(values["environment"]),
    )


def _namespace(tmp_path: Path) -> StateNamespace:
    root = tmp_path / ".vibesys" / "state" / "evaluation-agent"
    root.mkdir(parents=True)
    return StateNamespace(project_root=tmp_path, root=root, portable=False)


async def _submit_and_finish(service: EvaluationAgentService, token: str) -> str:
    submitted = await service.dispatch(
        SubmitCall(
            token=token,
            evidence_kinds=(EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
        )
    )
    assert isinstance(submitted, SubmittedReply)
    await service.dispatch(AwaitCall(token=token, handle_id=submitted.handle_id, timeout_s=3))
    return submitted.handle_id


@pytest.mark.asyncio
async def test_agent_results_are_reused_by_the_framework_gate_without_execution(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate()
    run.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
    run.evaluation.script_benchmark(
        BenchmarkEvaluation(
            executed=True,
            metric_name="throughput",
            metric_value=12.0,
            metric_direction=MetricDirection.MAXIMIZE,
            metric_unit="requests/s",
            row={"throughput": 12.0},
        )
    )
    namespace = _namespace(tmp_path)
    lifecycle: list[EvaluationLifecycleEvent] = []
    backend = SemanticEvaluationBackend(
        run.evaluation,
        run.workspaces,
        namespace,
        _identity(),
        events=lifecycle.append,
    )
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "throughput", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:throughput",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )

    handle_id = await _submit_and_finish(service, grant.token)
    snapshot = await backend.operation_snapshot(handle_id)
    assert snapshot.state is EvaluationState.SUCCEEDED
    assert snapshot.accepted_result
    assert len(snapshot.evidence_ids) == 2
    assert lifecycle[0].handle_id == handle_id
    assert lifecycle[-1].state is EvaluationState.SUCCEEDED
    assert len(run.evaluation.accuracy_calls) == 1
    assert len(run.evaluation.benchmark_calls) == 1

    # The framework records another commit after the agent turn. Its source patch is
    # unchanged, so content identity must survive the bookkeeping revision.
    framework_revision = await candidate.snapshot("framework gate")
    run.workspaces.set_patch(framework_revision, "patch for candidate-1-revision-1")
    evaluation = EvidenceReusingEvaluation(run.evaluation, backend, run_id=run.run_id)
    accuracy = await evaluation.accuracy(candidate)
    benchmark = await evaluation.benchmark(
        candidate,
        objectives=(BenchmarkObjective(name="throughput", direction=MetricDirection.MAXIMIZE),),
    )

    assert accuracy.passed
    assert not accuracy.executed
    assert benchmark.passed
    assert not benchmark.executed
    assert benchmark.metric_value == 12.0
    assert len(run.evaluation.accuracy_calls) == 1
    assert len(run.evaluation.benchmark_calls) == 1

    await candidate.snapshot("changed candidate")
    await evaluation.accuracy(candidate)
    assert len(run.evaluation.accuracy_calls) == 2
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_field", ["evaluator", "workload", "environment"])
async def test_reuse_rejects_non_candidate_identity_mismatches(
    tmp_path: Path,
    identity_field: str,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate()
    run.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "correctness", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:correctness",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    await service.dispatch(AwaitCall(token=grant.token, handle_id=submitted.handle_id, timeout_s=3))

    framework_revision = await candidate.snapshot("framework gate")
    run.workspaces.set_patch(framework_revision, "patch for candidate-1-revision-1")
    mismatched = SemanticEvaluationBackend(
        run.evaluation,
        run.workspaces,
        namespace,
        _identity(**{identity_field: "different"}),
    )
    evaluation = EvidenceReusingEvaluation(run.evaluation, mismatched, run_id=run.run_id)
    await evaluation.accuracy(candidate)

    assert len(run.evaluation.accuracy_calls) == 2
    await backend.close()
    await mismatched.close()
