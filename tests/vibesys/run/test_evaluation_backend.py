"""Public composition behavior for trusted evaluation reuse."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vs_evaluation.api import (
    EVALUATION_ACCESS_STATE_PATH,
    AwaitCall,
    AwaitReply,
    ContentDigest,
    EvaluationAdmissionStoppedError,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationAgentState,
    EvaluationCompleted,
    EvaluationDependencyError,
    EvaluationFailed,
    EvaluationLifecycleEvent,
    EvaluationRequest,
    EvaluationState,
    EvaluationStateNamespace,
    EvaluationStepResult,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    FailureKind,
    OwnedEvaluationDependencies,
    PartialMeasurement,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    ScopeClosingError,
    ScopeLifecycleStore,
    ScopePhase,
    ScopeRelease,
    ScopeReleasedReply,
    ScopeState,
    SettlementErrorCode,
    StageState,
    SubmitCall,
    SubmittedReply,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationExecutor,
    FakeProfilerTurnProvision,
    InMemoryEvaluationNamespace,
)
from vs_project.api import StateNamespace
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentEvaluationStageOutcome,
    AgentEvaluationStatus,
    AgentToolBindingContext,
    BenchmarkEvaluation,
    BenchmarkObjective,
    CandidateProfileStatus,
    MetricDirection,
    ReleasedJobs,
)
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pydantic import BaseModel


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


def _namespace(
    tmp_path: Path, namespace_type: type[StateNamespace] = StateNamespace
) -> StateNamespace:
    root = tmp_path / ".vibesys" / "state" / "evaluation-agent"
    root.mkdir(parents=True)
    return namespace_type(project_root=tmp_path, root=root, portable=False)


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
    assert snapshot.evidence_recorded
    assert len(snapshot.evidence_ids) == 2
    assert lifecycle[0].handle_id == handle_id
    assert lifecycle[-1].state is EvaluationState.SUCCEEDED
    assert len(run.evaluation.accuracy_calls) == 1
    assert len(run.evaluation.benchmark_calls) == 1

    # The framework records another commit after the agent turn. Its source patch is
    # unchanged, so content identity must survive the bookkeeping revision.
    framework_revision = await candidate.snapshot("framework gate")
    run.workspaces.set_patch(framework_revision, "patch for candidate-1-revision-1")
    evaluation = EvidenceReusingEvaluation(
        run.evaluation, backend, run_id=run.run_id, scopes=service
    )
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
    evaluation = EvidenceReusingEvaluation(
        run.evaluation, mismatched, run_id=run.run_id, scopes=service
    )
    await evaluation.accuracy(candidate)

    assert len(run.evaluation.accuracy_calls) == 2
    await backend.close()
    await mismatched.close()


@pytest.mark.asyncio
async def test_failed_accuracy_reaches_the_agent_and_skips_the_benchmark(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate()
    run.evaluation.script_accuracy(
        AccuracyEvaluation(executed=True, feedback="server failed to start: model not found")
    )
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "throughput", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:throughput",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    submitted = await service.dispatch(
        SubmitCall(
            token=grant.token,
            evidence_kinds=(EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
        )
    )
    assert isinstance(submitted, SubmittedReply)

    reply = await service.dispatch(
        AwaitCall(token=grant.token, handle_id=submitted.handle_id, timeout_s=3)
    )

    assert isinstance(reply, AwaitReply)
    assert isinstance(reply.result, EvaluationFailed)
    assert "server failed to start: model not found" in reply.result.message
    assert await backend.status(submitted.handle_id) is EvaluationState.FAILED
    assert run.evaluation.benchmark_calls == []
    await backend.close()


@pytest.mark.asyncio
async def test_policy_reads_the_outcomes_agents_submitted_from_a_workspace(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="cache")
    other = await run.workspaces.create_candidate(member_id="other")
    run.evaluation.script_accuracy(
        AccuracyEvaluation(executed=True, feedback="ValueError: length 22 exceeds capacity 21"),
        AccuracyEvaluation(executed=True),
    )
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "cache", str))
    backend.bind(AgentToolBindingContext(IMPLEMENTER, other, "other", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:cache",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    evaluation = EvidenceReusingEvaluation(
        run.evaluation, backend, run_id=run.run_id, scopes=service
    )

    await _submit_and_finish(service, grant.token)
    await candidate.snapshot("fixed the capacity")
    await _submit_and_finish(service, grant.token)

    outcomes = await evaluation.agent_evaluations(candidate)
    assert [item.status for item in outcomes] == [
        AgentEvaluationStatus.FAILED,
        AgentEvaluationStatus.PASSED,
    ]
    failure = outcomes[0].failure
    assert failure is not None
    assert "length 22 exceeds capacity 21" in failure
    assert outcomes[0].kinds == ("accuracy", "benchmark")
    assert outcomes[0].revision != outcomes[1].revision
    assert await evaluation.agent_evaluations(other) == ()
    await backend.close()


@pytest.mark.asyncio
async def test_recorded_evidence_reports_each_stage_outcome_not_a_pass(
    tmp_path: Path,
) -> None:
    # Regression: an evaluation whose benchmark ran and failed was reported as
    # an accepted result, and a planner read it as passing both gates.
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="cache")
    run.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
    run.evaluation.script_benchmark(
        BenchmarkEvaluation(
            executed=True,
            feedback="warmup timed out at 16.3 requests/s; 79.7 needed",
            metric_name="throughput",
            metric_value=16.3,
            metric_direction=MetricDirection.MAXIMIZE,
            metric_unit="requests/s",
            row={"throughput": 16.3},
        )
    )
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "cache", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:cache",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    evaluation = EvidenceReusingEvaluation(
        run.evaluation, backend, run_id=run.run_id, scopes=service
    )

    handle_id = await _submit_and_finish(service, grant.token)

    snapshot = await backend.operation_snapshot(handle_id)
    assert snapshot.evidence_recorded
    assert [(item.kind, item.outcome) for item in snapshot.stage_outcomes] == [
        (EvidenceKind.ACCURACY, EvidenceOutcome.PASSED),
        (EvidenceKind.BENCHMARK, EvidenceOutcome.FAILED),
    ]
    benchmark = snapshot.stage_outcomes[1]
    assert [(item.name, item.value, item.unit) for item in benchmark.metrics] == [
        ("throughput", 16.3, "requests/s")
    ]
    assert benchmark.summary_tail == "warmup timed out at 16.3 requests/s; 79.7 needed"
    (outcome,) = await evaluation.agent_evaluations(candidate)
    assert outcome.status is AgentEvaluationStatus.FAILED
    assert [(item.kind, item.outcome) for item in outcome.stages] == [
        ("accuracy", AgentEvaluationStageOutcome.PASSED),
        ("benchmark", AgentEvaluationStageOutcome.FAILED),
    ]
    assert [(item.name, item.value) for item in outcome.stages[1].metrics] == [("throughput", 16.3)]
    await backend.close()


def _server_failure(capacity: int) -> str:
    return (
        "Traceback (most recent call last):\n"
        f'  File "/stage/{capacity}/engine/model.py", line 442, in forward\n'
        "    raise ValueError(message)\n"
        f"ValueError: sequence length {capacity + 1} exceeds state capacity {capacity}\n"
    )


@pytest.mark.asyncio
async def test_the_await_reply_says_when_a_failure_repeats_the_previous_ones(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="cache")
    run.evaluation.script_accuracy(
        AccuracyEvaluation(executed=True, feedback=_server_failure(21)),
        AccuracyEvaluation(executed=True, feedback=_server_failure(30)),
        AccuracyEvaluation(executed=True),
        AccuracyEvaluation(executed=True, feedback=_server_failure(21)),
    )
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "cache", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:cache",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )

    async def submit_and_await(label: str) -> AwaitReply:
        await candidate.snapshot(label)
        submitted = await service.dispatch(
            SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(submitted, SubmittedReply)
        reply = await service.dispatch(
            AwaitCall(token=grant.token, handle_id=submitted.handle_id, timeout_s=3)
        )
        assert isinstance(reply, AwaitReply)
        return reply

    first = await submit_and_await("guess 1")
    second = await submit_and_await("guess 2")
    passed = await submit_and_await("fixed")
    after_pass = await submit_and_await("regressed")

    assert first.repeated_failure is None
    assert second.repeated_failure is not None
    assert second.repeated_failure.signature == "ValueError at model.py:442"
    assert second.repeated_failure.count == 2
    assert "read the code at the cited file and line" in second.repeated_failure.instruction
    assert passed.repeated_failure is None
    assert after_pass.repeated_failure is None
    evaluation = EvidenceReusingEvaluation(
        run.evaluation, backend, run_id=run.run_id, scopes=service
    )
    signatures = [item.signature for item in await evaluation.agent_evaluations(candidate)]
    assert signatures == [
        "ValueError at model.py:442",
        "ValueError at model.py:442",
        None,
        "ValueError at model.py:442",
    ]
    await backend.close()


def _warmup_stop(rate: float) -> BenchmarkEvaluation:
    return BenchmarkEvaluation(
        executed=True,
        feedback=f"warmup sub-run stopped: {rate} output tokens/s achieved",
        partial_measurement=PartialMeasurement(
            name="warmup_output_tokens_per_s",
            value=rate,
            direction="max",
            unit="output tokens/s",
            target=79.7,
        ),
    )


@pytest.mark.asyncio
async def test_the_await_reply_says_when_a_benchmark_stops_at_the_same_rate_again(
    tmp_path: Path,
) -> None:
    """Regression for r19: repeated warmup stops behind a passing accuracy stage never repeated.

    Each passed accuracy stage ended the run of failures, and a stop without a
    traceback had no signature at all.
    """
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="cache")
    run.evaluation.script_accuracy(*(AccuracyEvaluation(executed=True) for _ in range(3)))
    run.evaluation.script_benchmark(_warmup_stop(7.1), _warmup_stop(7.9), _warmup_stop(16.3))
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "cache", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:cache",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    replies: list[AwaitReply] = []
    for label in ("first", "same range", "faster"):
        await candidate.snapshot(label)
        submitted = await service.dispatch(
            SubmitCall(
                token=grant.token,
                evidence_kinds=(EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK),
            )
        )
        assert isinstance(submitted, SubmittedReply)
        reply = await service.dispatch(
            AwaitCall(token=grant.token, handle_id=submitted.handle_id, timeout_s=3)
        )
        assert isinstance(reply, AwaitReply)
        replies.append(reply)

    first, same_range, faster = (reply.repeated_failure for reply in replies)
    assert first is None
    assert same_range is not None
    assert (same_range.kind, same_range.stage, same_range.signature, same_range.count) == (
        FailureKind.MEASUREMENT,
        EvidenceKind.BENCHMARK,
        "warmup_output_tokens_per_s in [4, 8) output tokens/s",
        2,
    )
    assert faster is None
    await backend.close()


@pytest.mark.asyncio
async def test_resubmitting_unchanged_content_from_a_new_snapshot_joins_the_first_evaluation(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate()
    # Every snapshot below has the same content; only the commits differ.
    run.workspaces.set_default_patch("diff --git a/engine.py b/engine.py")
    run.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "throughput", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:throughput",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )

    first = await _submit_and_finish(service, grant.token)
    second = await _submit_and_finish(service, grant.token)

    assert second == first
    assert (await backend.operation_snapshot(second)).state is EvaluationState.SUCCEEDED
    assert len(run.evaluation.accuracy_calls) == 1
    await backend.close()


@dataclass
class _OwnedFakeExecutor(FakeEvaluationExecutor):
    """The shared executor Fake with the owned-executor close the backend requires."""

    async def close(self) -> None:
        """Hold no resources beyond the in-memory Fake."""


@pytest.mark.asyncio
async def test_recorded_snapshot_preserves_submission_identity_without_refresh() -> None:
    """Recovery reads the submitted candidate and generation before observing a job."""
    root = Path("/memory/settlement-record")
    run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="record")
    assert candidate.id is not None
    namespace = InMemoryEvaluationNamespace()
    executor = _OwnedFakeExecutor(clock=FakeClock())
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )
    revision = await candidate.snapshot("submitted")
    submitted = await backend.submit_revision_evidence(
        revision, (EvidenceKind.ACCURACY,), scope_id=candidate.id
    )
    before = await backend.recorded_snapshot(submitted.handle_id)
    executor.set_state(submitted.handle_id, EvaluationState.FAILED, failure="remote failure")
    await candidate.snapshot("new-live-content")

    # A durable read cannot refresh the remote failure or retarget the submission.
    assert await backend.recorded_snapshot(submitted.handle_id) == before
    assert before.request.owner_scope == candidate.id
    assert before.request.owner_generation == 0
    payload = before.request.stages[0].payload
    assert isinstance(payload, dict)
    assert payload["snapshot"] == revision
    assert payload["fingerprints"] == submitted.fingerprints.model_dump(mode="json")
    assert await backend.status(submitted.handle_id) is EvaluationState.FAILED

    recovered = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )
    terminal = await recovered.recorded_snapshot(submitted.handle_id)
    assert terminal.state is EvaluationState.FAILED
    assert terminal.request == before.request
    await recovered.close()
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [EvaluationState.SUCCEEDED, EvaluationState.FAILED])
async def test_service_settlements_keep_real_submission_identity_after_live_revision_changes(
    terminal: EvaluationState,
) -> None:
    """Lifecycle consumers receive the actual evaluation owner and frozen measurement."""
    root = Path("/memory/semantic-settlements")
    run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="logical-hypothesis")
    assert candidate.id is not None
    assert candidate.id != "logical-hypothesis"
    namespace = InMemoryEvaluationNamespace()
    executor = _OwnedFakeExecutor(
        clock=FakeClock(), supported_evidence_kinds=(EvidenceKind.ACCURACY.value,)
    )
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "logical-hypothesis", str))
    service = EvaluationAgentService(backend, namespace, root / "unused.sock")
    grant = service.grant(
        principal_id="implementer:logical-hypothesis",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    record = await backend.recorded_snapshot(submitted.handle_id)
    payload = record.request.stages[0].payload
    assert isinstance(payload, dict)
    fingerprints = EvidenceFingerprints.model_validate(payload["fingerprints"])
    evidence = TrustedEvidence(
        evidence_id="a" * 64,
        evaluation_id=submitted.handle_id,
        stage_name="accuracy",
        kind=EvidenceKind.ACCURACY,
        fingerprints=fingerprints,
        trusted_inputs=fingerprints.candidate,
        outcome=EvidenceOutcome.PASSED,
        accepted_round=0,
    )
    stage = EvaluationStepResult(
        name="accuracy", state=StageState.SUCCEEDED, result=evidence.model_dump(mode="json")
    )
    dependencies = OwnedEvaluationDependencies(
        scope_id=candidate.id, generation=0, handles=(submitted.handle_id,)
    )
    settlements = service.settlements()
    (pending,) = await settlements.observe(dependencies)
    assert (await backend.recorded_submission(submitted.handle_id)).fingerprints == fingerprints
    run.workspaces.set_default_patch("diff --git a/changed.py b/changed.py")
    await candidate.snapshot("different-live-candidate")
    executor.set_state(
        submitted.handle_id,
        terminal,
        stage_results=(stage,),
        failure="later-stage failure" if terminal is EvaluationState.FAILED else None,
    )
    (settled,) = await settlements.wait_any(dependencies)
    assert settled.scope_id == candidate.id
    assert settled.generation == 0
    assert settled.fingerprints == pending.fingerprints == fingerprints
    assert settled.fingerprints.evaluator == _identity().evaluator
    durable = await backend.recorded_snapshot(submitted.handle_id)
    assert durable.request == record.request
    assert durable.stage_results == (stage,)
    assert payload["snapshot"] != candidate.revision
    if terminal is EvaluationState.SUCCEEDED:
        assert isinstance(settled.result, EvaluationCompleted)
        assert settled.result.stages == (stage,)
    else:
        assert isinstance(settled.result, EvaluationFailed)
        assert settled.result.message == "later-stage failure"
    assert executor.cancellations == []
    await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_field", ["candidate", "evaluator", "workload", "environment"])
async def test_settlements_reject_access_identity_that_disagrees_with_submitted_capture(
    identity_field: str,
) -> None:
    """A corrupt grant cannot relabel terminal evidence from another candidate."""
    root = Path("/memory/settlement-access-conflict")
    run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="identity")
    assert candidate.id is not None
    harness = _release_harness(root, run, namespace=InMemoryEvaluationNamespace())
    harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "identity", str))
    submitted = await harness.submit(candidate.id, EvidenceKind.ACCURACY)
    assert isinstance(submitted, SubmittedReply)
    original = await harness.backend.recorded_submission(submitted.handle_id)
    access = harness.namespace.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
    (handle,) = access.handles
    corrupt = handle.fingerprints.model_copy(update={identity_field: _digest("different identity")})
    harness.namespace.save(
        EVALUATION_ACCESS_STATE_PATH,
        EvaluationAgentState(handles=(handle.model_copy(update={"fingerprints": corrupt}),)),
    )
    harness.executor.set_state(submitted.handle_id, EvaluationState.FAILED, failure="failed")
    await harness.backend.status(submitted.handle_id)
    dependencies = OwnedEvaluationDependencies(
        scope_id=candidate.id, generation=0, handles=(submitted.handle_id,)
    )
    with pytest.raises(EvaluationDependencyError) as error:
        await harness.service.settlements().observe(dependencies)
    assert error.value.code is SettlementErrorCode.IDENTITY_CONFLICT
    assert error.value.handle_id == submitted.handle_id
    assert await harness.backend.recorded_submission(submitted.handle_id) == original
    assert harness.executor.cancellations == []
    await harness.profiler.close()
    await harness.backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [EvaluationState.FAILED, EvaluationState.CANCELED])
async def test_unchanged_content_is_measured_again_after_an_attempt_without_a_result(
    tmp_path: Path,
    ended: EvaluationState,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate()
    run.workspaces.set_default_patch("diff --git a/engine.py b/engine.py")
    executor = _OwnedFakeExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=(EvidenceKind.ACCURACY.value, EvidenceKind.BENCHMARK.value),
    )
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "throughput", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:throughput",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    call = SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.BENCHMARK,))

    first = await service.dispatch(call)
    assert isinstance(first, SubmittedReply)
    failure = "node lost" if ended is EvaluationState.FAILED else None
    executor.set_state(first.handle_id, ended, failure=failure)
    assert (await backend.operation_snapshot(first.handle_id)).state is ended
    second = await service.dispatch(call)
    third = await service.dispatch(call)

    assert isinstance(second, SubmittedReply)
    assert isinstance(third, SubmittedReply)
    assert second.handle_id != first.handle_id
    assert third.handle_id == second.handle_id
    assert len(executor.submissions) == 2
    await backend.close()


@pytest.mark.asyncio
async def test_concurrent_submissions_of_unchanged_content_share_one_evaluation(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate()
    run.workspaces.set_default_patch("diff --git a/engine.py b/engine.py")
    executor = _OwnedFakeExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=(EvidenceKind.ACCURACY.value, EvidenceKind.BENCHMARK.value),
    )
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "throughput", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:throughput",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    call = SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.BENCHMARK,))

    replies = await asyncio.gather(*(service.dispatch(call) for _ in range(4)))

    handles = {reply.handle_id for reply in replies if isinstance(reply, SubmittedReply)}
    assert len(handles) == 1
    assert all(isinstance(reply, SubmittedReply) for reply in replies)
    assert len(executor.submissions) == 1
    await backend.close()


@dataclass
class _BlockingSubmitExecutor(_OwnedFakeExecutor):
    """An executor whose submit stays in progress until the test releases it."""

    release: asyncio.Event = field(default_factory=asyncio.Event)
    entered: list[str] = field(default_factory=list)
    arrivals: asyncio.Queue[str] = field(default_factory=asyncio.Queue)

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Record the arrival, then wait for the release like a slow remote stage."""
        self.entered.append(handle_id)
        self.arrivals.put_nowait(handle_id)
        await self.release.wait()
        await super().submit(request, handle_id=handle_id)


@pytest.mark.asyncio
async def test_slow_submission_does_not_delay_a_submission_of_different_content(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate()
    executor = _BlockingSubmitExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=(EvidenceKind.ACCURACY.value, EvidenceKind.BENCHMARK.value),
    )
    namespace = _namespace(tmp_path)
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )
    backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "throughput", str))
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
    grant = service.grant(
        principal_id="implementer:throughput",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=candidate.id,
    )
    call = SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.BENCHMARK,))

    # Each snapshot is a new revision with its own patch, so the content differs.
    submissions = [asyncio.create_task(service.dispatch(call)) for _ in range(2)]
    await executor.arrivals.get()
    await executor.arrivals.get()

    assert len(set(executor.entered)) == 2
    executor.release.set()
    replies = await asyncio.gather(*submissions)
    assert len({reply.handle_id for reply in replies if isinstance(reply, SubmittedReply)}) == 2
    await backend.close()


@dataclass
class _ReleaseHarness:
    executor: _OwnedFakeExecutor
    backend: SemanticEvaluationBackend
    service: EvaluationAgentService
    profiler: ProfilerAgentService
    provision: FakeProfilerTurnProvision
    namespace: EvaluationStateNamespace

    async def submit(self, workspace_id: str | None, kind: EvidenceKind) -> object:
        grant = self.service.grant(
            principal_id=f"implementer:{workspace_id}",
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=workspace_id,
        )
        return await self.service.dispatch(SubmitCall(token=grant.token, evidence_kinds=(kind,)))


def _release_harness(
    tmp_path: Path,
    run: FakeRun,
    namespace: EvaluationStateNamespace | None = None,
    executor: _OwnedFakeExecutor | None = None,
) -> _ReleaseHarness:
    executor = executor or _OwnedFakeExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=tuple(kind.value for kind in EvidenceKind),
        advance_clock_on_timeout=False,
    )
    namespace = namespace or _namespace(tmp_path)
    backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=executor
    )
    provision = FakeProfilerTurnProvision()

    async def no_evidence(
        _principal: str, _scope: str | None, _snapshot: str, _ids: tuple[str, ...]
    ) -> tuple[TrustedEvidence, ...]:
        return ()

    profiler = ProfilerAgentService(
        provision,
        namespace,
        ProfilerAgentServiceHooks(
            partial(backend.snapshot, label="profiler-agent-dispatch"), no_evidence
        ),
    )
    service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock", profiler)
    return _ReleaseHarness(executor, backend, service, profiler, provision, namespace)


@pytest.mark.asyncio
async def test_release_jobs_cancels_the_members_jobs_and_its_running_profile_only(
    tmp_path: Path,
) -> None:
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    run.workspaces.set_default_patch("diff --git a/engine.py b/engine.py")
    released = await run.workspaces.create_candidate(member_id="released")
    kept = await run.workspaces.create_candidate(member_id="kept")
    harness = _release_harness(tmp_path, run)
    evaluation = EvidenceReusingEvaluation(
        run.evaluation,
        harness.backend,
        run_id=run.run_id,
        scopes=harness.service,
        profiler=harness.profiler,
    )
    for workspace in (released, kept):
        harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, "member", str))
    own = await harness.submit(released.id, EvidenceKind.BENCHMARK)
    other = await harness.submit(kept.id, EvidenceKind.ACCURACY)
    assert isinstance(own, SubmittedReply)
    assert isinstance(other, SubmittedReply)
    revision = await released.snapshot("profile")
    dispatched = await harness.profiler.dispatch(
        principal_id="released",
        scope_id=released.id,
        request="Where does time go?",
        work=ProfilerWorkKey(purpose=ProfilerWorkPurpose.PLANNING_GUIDANCE, focus="decode"),
        session_id=None,
        candidate_snapshot_id=revision,
    )
    await harness.provision.wait_started(dispatched.operation_id)
    (turn,) = harness.provision.turns

    release = await evaluation.release_jobs("released")

    assert release == ReleasedJobs(
        member_id="released",
        evaluations=(own.handle_id,),
        profiler_operations=(turn.operation_id,),
        first_release=True,
    )
    outcome = await harness.profiler.await_result(dispatched.operation_id, "released", None, 1.0)
    assert outcome.operation.state.value == "canceled"
    assert harness.executor.cancellations == [own.handle_id]
    assert (await harness.backend.status(other.handle_id)) is EvaluationState.QUEUED
    assert await harness.submit(released.id, EvidenceKind.ACCURACY) == ScopeReleasedReply()
    refused = await evaluation.profile(revision, "Again?", member_id="released")
    assert refused.status is CandidateProfileStatus.FAILED
    assert harness.provision.turns == [turn]
    assert await evaluation.release_jobs("released") == ReleasedJobs(
        member_id="released", evaluations=(), profiler_operations=(), first_release=False
    )
    await harness.profiler.close()
    await harness.backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_cancelled_profile_wait_keeps_capture_owned_until_release(
    tmp_path: Path,
    *,
    restart: bool,
) -> None:
    """Finding 2: cancelling the caller cannot forget its live trusted capture."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="capture")
    harness = _release_harness(tmp_path, run)
    evaluation = EvidenceReusingEvaluation(
        run.evaluation,
        harness.backend,
        run_id=run.run_id,
        scopes=harness.service,
        profiler=harness.profiler,
    )
    profile = asyncio.create_task(
        evaluation.profile(
            await candidate.snapshot("capture"), "Decode profile", member_id="capture"
        )
    )
    await harness.executor.wait_started.wait()
    (capture,) = harness.executor.submissions
    profile.cancel()
    with pytest.raises(asyncio.CancelledError):
        await profile
    if restart:
        harness.service = EvaluationAgentService(
            harness.backend, harness.namespace, tmp_path / "resumed.sock", harness.profiler
        )
        evaluation = EvidenceReusingEvaluation(
            run.evaluation,
            harness.backend,
            run_id=run.run_id,
            scopes=harness.service,
            profiler=harness.profiler,
        )
    await evaluation.release_jobs("capture")
    assert await harness.backend.status(capture.handle_id) is EvaluationState.CANCELED
    assert harness.executor.backend.active_count == 0
    await harness.profiler.close()
    await harness.backend.close()


@pytest.mark.asyncio
async def test_incomplete_scope_release_retries_cancellation_after_backend_failure(
    tmp_path: Path,
) -> None:
    """Finding 3: an intent marker cannot suppress unfinished cancellation."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="retry")
    assert candidate.id is not None
    harness = _release_harness(tmp_path, run)
    harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "retry", str))
    submitted = await harness.submit(candidate.id, EvidenceKind.ACCURACY)
    assert isinstance(submitted, SubmittedReply)
    harness.executor.fail_cancel_once = True
    with pytest.raises(OSError, match=r"^$"):
        await harness.service.cancel_scope(candidate.id)
    resumed = EvaluationAgentService(
        harness.backend, harness.namespace, tmp_path / "retry.sock", harness.profiler
    )
    release = await resumed.cancel_scope(candidate.id)
    assert not release.first_release
    assert harness.executor.backend.active_count == 0
    assert await harness.backend.status(submitted.handle_id) is EvaluationState.CANCELED
    await harness.profiler.close()
    await harness.backend.close()


async def _capture_release_trace(root: Path, actions: list[str]) -> None:
    run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="trace")
    assert candidate.id is not None
    harness = _release_harness(root, run, namespace=InMemoryEvaluationNamespace())
    evaluation = EvidenceReusingEvaluation(
        run.evaluation,
        harness.backend,
        run_id=run.run_id,
        scopes=harness.service,
        profiler=harness.profiler,
    )
    profile = asyncio.create_task(
        evaluation.profile(await candidate.snapshot("trace"), "Decode profile", member_id="trace")
    )
    await harness.executor.wait_started.wait()
    profile.cancel()
    with pytest.raises(asyncio.CancelledError):
        await profile
    for action in actions:
        if action == "crash":
            harness.service = EvaluationAgentService(
                harness.backend, harness.namespace, root / "restart.sock", harness.profiler
            )
        elif action == "fault":
            harness.executor.fail_cancel_once = True
            with suppress(OSError):
                await harness.service.cancel_scope(candidate.id)
        elif action == "stop":
            await harness.service.cancel_outstanding()
        else:
            await harness.service.cancel_scope(candidate.id)
    harness.executor.fail_cancel_once = False
    await harness.service.cancel_scope(candidate.id)
    assert harness.executor.backend.active_count == 0
    for job in harness.executor.submissions:
        assert await harness.backend.status(job.handle_id) is EvaluationState.CANCELED
    scope = next(
        scope
        for scope in ScopeLifecycleStore(harness.namespace).snapshot().scopes
        if scope.scope_id == candidate.id
    )
    assert scope.phase is ScopePhase.CLOSED
    assert await harness.service.cancel_scope(candidate.id) == ScopeRelease(
        scope_id=candidate.id, first_release=False
    )
    await harness.profiler.close()
    await harness.backend.close()


@settings(max_examples=20)
@given(actions=st.lists(st.sampled_from(("crash", "fault", "stop", "release")), max_size=8))
def test_profile_capture_release_recovers_across_fault_and_restart_sequences(
    actions: list[str],
) -> None:
    """Finding 9: profile-capable cleanup remains replayable at failure boundaries."""
    asyncio.run(_capture_release_trace(Path("/memory/profile-release"), actions))


class _CommitAcknowledgementLostError(OSError):
    """An injected crash after durable storage replacement."""

    def __init__(self) -> None:
        """Name the generic storage-boundary fault."""
        super().__init__("storage commit acknowledgement lost")


class _SaveFaultNamespace(StateNamespace):
    """Storage boundary wrapper that crashes after a selected committed write."""

    target_suffix: str = ""
    target_ordinal: int = 1
    matches: int = 0

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Forward the same write, then lose its acknowledgement at a configured point."""
        super().save(relative_path, model)
        if self.target_suffix and str(relative_path).endswith(self.target_suffix):
            self.matches += 1
            if self.matches == self.target_ordinal:
                self.target_suffix = ""
                raise _CommitAcknowledgementLostError


@pytest.mark.asyncio
async def test_claim_without_access_acknowledgement_remains_owned_on_restart(
    tmp_path: Path,
) -> None:
    """Claim ownership survives a crash before the access record or executor submit."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="claim")
    assert candidate.id is not None
    namespace = _namespace(tmp_path, _SaveFaultNamespace)
    assert isinstance(namespace, _SaveFaultNamespace)
    harness = _release_harness(tmp_path, run, namespace)
    harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "claim", str))
    # test-isolation: inject lost acknowledgement at the store's claim commit,
    # before ownership is projected into agent access state.
    namespace.target_suffix = "/index.json"
    with pytest.raises(OSError, match="storage commit acknowledgement lost"):
        await harness.submit(candidate.id, EvidenceKind.ACCURACY)
    assert not harness.executor.submissions
    await harness.service.cancel_scope(candidate.id)
    resumed_backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, namespace, _identity(), executor=harness.executor
    )
    await resumed_backend.start()
    assert not harness.executor.submissions
    assert harness.executor.backend.active_count == 0
    await harness.profiler.close()
    await resumed_backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinal", [1, 2])
async def test_release_commit_acknowledgement_loss_replays_without_dispatch(
    tmp_path: Path,
    ordinal: int,
) -> None:
    """Intent and completion commits tolerate a crash after durable replacement."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="commit")
    assert candidate.id is not None
    namespace = _namespace(tmp_path, _SaveFaultNamespace)
    assert isinstance(namespace, _SaveFaultNamespace)
    harness = _release_harness(tmp_path, run, namespace)
    harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "commit", str))
    submitted = await harness.submit(candidate.id, EvidenceKind.ACCURACY)
    assert isinstance(submitted, SubmittedReply)
    namespace.target_suffix = "agent-evaluation-released-scopes.json"
    namespace.target_ordinal = ordinal
    with pytest.raises(OSError, match="storage commit acknowledgement lost"):
        await harness.service.cancel_scope(candidate.id)
    resumed_service = EvaluationAgentService(
        harness.backend, namespace, tmp_path / "commit.sock", harness.profiler
    )
    await resumed_service.cancel_scope(candidate.id)
    assert await harness.backend.status(submitted.handle_id) is EvaluationState.CANCELED
    assert harness.executor.backend.active_count == 0
    assert len(harness.executor.submissions) == 1
    await harness.profiler.close()
    await harness.backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [None, "summary", "evaluator"])
async def test_profile_references_resolve_once_when_two_scopes_record_identical_evidence(
    tmp_path: Path,
    conflict: str | None,
) -> None:
    """Resource ownership is separate even when immutable evidence identity is shared."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="evidence")
    assert candidate.id is not None
    harness = _release_harness(tmp_path, run)
    revision = await candidate.snapshot("profile")
    first = await harness.backend.submit_revision_evidence(
        revision, (EvidenceKind.PROFILE,), scope_id="one"
    )
    second = await harness.backend.submit_revision_evidence(
        revision, (EvidenceKind.PROFILE,), scope_id="two"
    )
    assert first.handle_id != second.handle_id
    evidence = TrustedEvidence(
        evidence_id="a" * 64,
        evaluation_id="a" * 64,
        stage_name="profile",
        kind=EvidenceKind.PROFILE,
        fingerprints=first.fingerprints,
        trusted_inputs=first.fingerprints.candidate,
        outcome=EvidenceOutcome.PASSED,
        accepted_round=0,
    )
    changed = evidence
    if conflict == "summary":
        changed = evidence.model_copy(update={"semantic_summary": "conflicting measurement"})
    elif conflict == "evaluator":
        changed = evidence.model_copy(
            update={
                "fingerprints": evidence.fingerprints.model_copy(
                    update={"evaluator": _digest("different evaluator")}
                )
            }
        )
    for submitted, observed in ((first, evidence), (second, changed)):
        harness.executor.set_state(
            submitted.handle_id,
            EvaluationState.SUCCEEDED,
            stage_results=(
                EvaluationStepResult(
                    name="profile",
                    state=StageState.SUCCEEDED,
                    result=observed.model_dump(mode="json"),
                ),
            ),
        )
        await harness.backend.operation_snapshot(submitted.handle_id)
    if conflict is not None:
        with pytest.raises(ValueError, match="identifies conflicting content"):
            await harness.backend.resolve_profile_evidence(
                "profiler", "two", revision, (evidence.evidence_id,)
            )
    else:
        resolved = await harness.backend.resolve_profile_evidence(
            "profiler", "two", revision, (evidence.evidence_id,)
        )
        assert resolved == (evidence,)
        harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "evidence", str))
        assert await harness.backend.accepted_evidence(candidate.id, (EvidenceKind.PROFILE,)) == (
            evidence,
        )
    await harness.profiler.close()
    await harness.backend.close()


@dataclass
class _AcceptedSubmitBarrierExecutor(_OwnedFakeExecutor):
    """Executor barrier after remote acceptance, before submission acknowledgement."""

    accepted: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    acknowledgement: asyncio.Event = field(default_factory=asyncio.Event)

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Accept idempotently, then wait for the caller to observe the reply."""
        await super().submit(request, handle_id=handle_id)
        self.accepted.put_nowait(handle_id)
        await self.acknowledgement.wait()


@pytest.mark.asyncio
async def test_release_owns_submission_cancelled_after_remote_acceptance(tmp_path: Path) -> None:
    """A caller lost during submit cannot strand an accepted scoped job."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="accepted")
    assert candidate.id is not None
    executor = _AcceptedSubmitBarrierExecutor(
        clock=FakeClock(), supported_evidence_kinds=tuple(kind.value for kind in EvidenceKind)
    )
    harness = _release_harness(tmp_path, run, executor=executor)
    harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "accepted", str))
    submission = asyncio.create_task(harness.submit(candidate.id, EvidenceKind.ACCURACY))
    handle_id = await executor.accepted.get()
    submission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submission
    await harness.service.cancel_scope(candidate.id)
    executor.acknowledgement.set()
    resumed_backend = SemanticEvaluationBackend(
        run.evaluation, run.workspaces, harness.namespace, _identity(), executor=executor
    )
    await resumed_backend.start()
    assert executor.backend.active_count == 0
    assert await resumed_backend.recorded_status(handle_id) is EvaluationState.CANCELED
    assert len(executor.submissions) == 1
    await harness.profiler.close()
    await resumed_backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", ["release", "backend_restart"])
async def test_legacy_release_marker_reconciles_unowned_profile_claims_before_dispatch(
    tmp_path: Path,
    recover: str,
) -> None:
    """A v1 marker proves admission closure, not termination of legacy trusted captures."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="legacy")
    assert candidate.id is not None
    harness = _release_harness(tmp_path, run)
    submitted = await harness.backend.submit_revision_evidence(
        await candidate.snapshot("legacy"), (EvidenceKind.PROFILE,)
    )
    assert harness.executor.backend.active_count == 1
    assert isinstance(harness.namespace, StateNamespace)
    harness.namespace.write_bytes(
        "agent-evaluation-released-scopes.json",
        json.dumps({"schema_version": 1, "scope_ids": [candidate.id]}).encode(),
    )
    if recover == "backend_restart":
        resumed_backend = SemanticEvaluationBackend(
            run.evaluation,
            run.workspaces,
            harness.namespace,
            _identity(),
            executor=harness.executor,
        )
        await resumed_backend.start()
    else:
        await harness.service.cancel_scope(candidate.id)
    assert harness.executor.backend.active_count == 0
    assert await harness.backend.recorded_status(submitted.handle_id) is EvaluationState.CANCELED
    assert len(harness.executor.submissions) == 1
    await harness.profiler.close()
    await harness.backend.close()


@pytest.mark.asyncio
async def test_reopen_jobs_finishes_cleanup_before_fresh_generation_admission(
    tmp_path: Path,
) -> None:
    """A parked member cannot reuse its closed generation or bypass unfinished cleanup."""
    run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="reopen")
    assert candidate.id is not None
    harness = _release_harness(tmp_path, run)
    harness.backend.bind(AgentToolBindingContext(IMPLEMENTER, candidate, "reopen", str))
    evaluation = EvidenceReusingEvaluation(
        run.evaluation,
        harness.backend,
        run_id=run.run_id,
        scopes=harness.service,
        profiler=harness.profiler,
    )
    first = await harness.submit(candidate.id, EvidenceKind.ACCURACY)
    assert isinstance(first, SubmittedReply)
    harness.executor.fail_cancel_once = True
    with pytest.raises(OSError, match=r"^$"):
        await evaluation.release_jobs("reopen")
    assert await evaluation.jobs_released("reopen")
    await evaluation.reopen_jobs("reopen")
    assert not await evaluation.jobs_released("reopen")
    assert harness.executor.backend.active_count == 0
    second = await harness.submit(candidate.id, EvidenceKind.ACCURACY)
    assert isinstance(second, SubmittedReply)
    assert second.handle_id != first.handle_id
    (scope,) = ScopeLifecycleStore(harness.namespace).snapshot().scopes
    assert scope.generation == 1
    assert await harness.backend.recorded_status(first.handle_id) is EvaluationState.CANCELED
    await harness.service.cancel_scope(candidate.id)
    assert harness.executor.backend.active_count == 0
    await harness.profiler.close()
    await harness.backend.close()


class _ClosingBarrierNamespace(InMemoryEvaluationNamespace):
    """Expose the durable admission fence through a storage boundary event."""

    def __init__(self) -> None:
        super().__init__()
        self.closing = asyncio.Event()

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        super().save(relative_path, model)
        if isinstance(model, ScopeState) and any(
            scope.phase is ScopePhase.CLOSING for scope in model.scopes
        ):
            self.closing.set()


@pytest.mark.asyncio
async def test_release_drains_claimed_submission_before_closed_without_dispatch() -> None:
    """A release fence joins admitted claims before reporting cleanup complete."""
    root = Path("/memory/claim-release")
    run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="claim-release")
    assert candidate.id is not None
    namespace = _ClosingBarrierNamespace()
    harness = _release_harness(root, run, namespace=namespace)
    claimed = asyncio.Event()
    acknowledgement = asyncio.Event()

    async def own(_submitted: SubmittedSemanticEvaluation) -> None:
        claimed.set()
        await acknowledgement.wait()

    submit = asyncio.create_task(
        harness.backend.submit_revision_evidence(
            await candidate.snapshot("barrier"),
            (EvidenceKind.PROFILE,),
            scope_id=candidate.id,
            own=own,
        )
    )
    await claimed.wait()
    release = asyncio.create_task(harness.service.cancel_scope(candidate.id))
    await namespace.closing.wait()
    assert not release.done()
    assert not harness.executor.submissions
    acknowledgement.set()
    with pytest.raises(ScopeClosingError):
        await submit
    await release
    assert not harness.executor.submissions
    assert harness.executor.backend.active_count == 0
    assert ScopeLifecycleStore(namespace).snapshot().scopes[0].phase is ScopePhase.CLOSED
    await harness.profiler.close()
    await harness.backend.close()


class _StopBarrierBackend(SemanticEvaluationBackend):
    """Expose entry to the process stop admission effect boundary."""

    def __init__(
        self, run: FakeRun, namespace: EvaluationStateNamespace, executor: _OwnedFakeExecutor
    ) -> None:
        super().__init__(run.evaluation, run.workspaces, namespace, _identity(), executor=executor)
        self.stop_entered = asyncio.Event()

    async def drain_submissions(self, scope_id: str | None) -> None:
        if scope_id is None:
            self.stop_entered.set()
        await super().drain_submissions(scope_id)


@pytest.mark.asyncio
async def test_stop_drains_admitted_claim_and_refuses_later_dispatch() -> None:
    """A process stop cannot miss an admitted claim before its cancellation snapshot."""
    root = Path("/memory/claim-stop")
    run = FakeRun(PLUGIN, project_root=root, supports_parallel_candidates=True)
    candidate = await run.workspaces.create_candidate(member_id="claim-stop")
    assert candidate.id is not None
    harness = _release_harness(root, run, namespace=InMemoryEvaluationNamespace())
    backend = _StopBarrierBackend(run, harness.namespace, harness.executor)
    service = EvaluationAgentService(
        backend, harness.namespace, root / "stop.sock", harness.profiler
    )
    claimed = asyncio.Event()
    acknowledgement = asyncio.Event()

    async def own(_submitted: SubmittedSemanticEvaluation) -> None:
        claimed.set()
        await acknowledgement.wait()

    snapshot = await candidate.snapshot("barrier")
    submit = asyncio.create_task(
        backend.submit_revision_evidence(
            snapshot,
            (EvidenceKind.PROFILE,),
            scope_id=candidate.id,
            own=own,
        )
    )
    await claimed.wait()
    stop = asyncio.create_task(service.cancel_outstanding())
    await backend.stop_entered.wait()
    assert not stop.done()
    assert not harness.executor.submissions
    acknowledgement.set()
    with pytest.raises(EvaluationAdmissionStoppedError):
        await submit
    await stop
    assert not harness.executor.submissions
    assert harness.executor.backend.active_count == 0
    with pytest.raises(EvaluationAdmissionStoppedError):
        await backend.submit_revision_evidence(
            snapshot, (EvidenceKind.PROFILE,), scope_id=candidate.id
        )
    await harness.profiler.close()
    await backend.close()
