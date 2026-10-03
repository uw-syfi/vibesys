"""Public composition behavior for trusted evaluation reuse."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
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
    AwaitReply,
    ContentDigest,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationFailed,
    EvaluationLifecycleEvent,
    EvaluationRequest,
    EvaluationState,
    EvidenceKind,
    EvidenceOutcome,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ScopeReleasedReply,
    SubmitCall,
    SubmittedReply,
    TrustedEvidence,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationExecutor,
    FakeProfilerTurnProvision,
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

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        """Record the arrival, then wait for the release like a slow remote stage."""
        self.entered.append(handle_id)
        await self.release.wait()
        await super().submit(request, handle_id=handle_id)


async def _let_ready_tasks_run() -> None:
    # Every await in the code under test completes without real I/O, so a fixed
    # number of event-loop turns lets all runnable work reach its next real wait.
    for _ in range(100):
        await asyncio.sleep(0)


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
    await _let_ready_tasks_run()

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

    async def submit(self, workspace_id: str | None, kind: EvidenceKind) -> object:
        grant = self.service.grant(
            principal_id=f"implementer:{workspace_id}",
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=workspace_id,
        )
        return await self.service.dispatch(SubmitCall(token=grant.token, evidence_kinds=(kind,)))


def _release_harness(tmp_path: Path, run: FakeRun) -> _ReleaseHarness:
    executor = _OwnedFakeExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=(EvidenceKind.ACCURACY.value, EvidenceKind.BENCHMARK.value),
    )
    namespace = _namespace(tmp_path)
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
    return _ReleaseHarness(executor, backend, service, profiler, provision)


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
    profile = asyncio.ensure_future(
        evaluation.profile(revision, "Where does time go?", member_id="released")
    )
    await _let_ready_tasks_run()
    (turn,) = harness.provision.turns

    release = await evaluation.release_jobs("released")

    assert release == ReleasedJobs(
        member_id="released",
        evaluations=(own.handle_id,),
        profiler_operations=(turn.operation_id,),
        first_release=True,
    )
    outcome = await profile
    assert outcome.status is CandidateProfileStatus.FAILED
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
