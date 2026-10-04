"""Measured evaluation identity across requester scopes."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, PROFILER
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vs_evaluation.api import (
    CancelCall,
    ContentDigest,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationCompleted,
    EvaluationState,
    EvidenceKind,
    OwnedEvaluationDependencies,
    StoredEvaluation,
    SubmitCall,
    SubmittedReply,
)
from vs_evaluation.api.testing import FakeClock, FakeEvaluationExecutor, InMemoryEvaluationNamespace
from vs_runtime.api import (
    AccuracyEvaluation,
    AgentEvaluationStatus,
    AgentRole,
    AgentToolBindingContext,
    CandidateWorkspace,
    Run,
)
from vs_runtime.api.infrastructure import TrustedEvaluationPlan
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path


def _identity(**changes: str) -> SemanticEvaluationIdentity:
    values = {
        "evaluator": "evaluator",
        "workload": "workload",
        "environment": "environment",
        **changes,
    }
    return SemanticEvaluationIdentity(
        evaluator=ContentDigest.sha256(values["evaluator"].encode()),
        workload=ContentDigest.sha256(values["workload"].encode()),
        environment=ContentDigest.sha256(values["environment"].encode()),
    )


async def _candidate(run: FakeRun, member_id: str) -> CandidateWorkspace:
    candidate = await run.workspaces.create_candidate(member_id=member_id)
    await candidate.snapshot("same measured content")
    return candidate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "role", "agent_role"),
    [
        (EvidenceKind.ACCURACY, EvaluationAgentRole.IMPLEMENTER, IMPLEMENTER),
        (EvidenceKind.BENCHMARK, EvaluationAgentRole.IMPLEMENTER, IMPLEMENTER),
        (EvidenceKind.PROFILE, EvaluationAgentRole.PROFILER, PROFILER),
    ],
)
async def test_agent_workspace_submission_joins_same_measurement_across_scopes(
    tmp_path: Path,
    kind: EvidenceKind,
    role: EvaluationAgentRole,
    agent_role: AgentRole,
) -> None:
    async with AsyncExitStack() as cleanup:
        run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        run.workspaces.set_default_patch("same candidate patch")
        host = await _candidate(run, "host")
        agent = await _candidate(run, "agent")
        assert host.id is not None
        assert agent.id is not None
        namespace = InMemoryEvaluationNamespace()
        executor = FakeEvaluationExecutor(
            clock=FakeClock(), supported_evidence_kinds=tuple(item.value for item in EvidenceKind)
        )
        backend = SemanticEvaluationBackend(
            run.evaluation,
            run.workspaces,
            namespace,
            _identity(),
            executor=executor,
        )
        cleanup.push_async_callback(backend.close)
        backend.bind(AgentToolBindingContext(agent_role, agent, "same-content", str))
        service = EvaluationAgentService(backend, namespace, tmp_path / "evaluation.sock")
        cleanup.push_async_callback(service.close)

        host_revision = host.revision
        assert host_revision is not None
        host_submission = await backend.submit_revision_evidence(
            host_revision, (kind,), scope_id=host.id
        )
        grant = service.grant(principal_id="agent", role=role, scope_id=agent.id)
        reply = await service.dispatch(SubmitCall(token=grant.token, evidence_kinds=(kind,)))

        assert isinstance(reply, SubmittedReply)
        assert reply.handle_id == host_submission.handle_id
        assert len(executor.submissions) == 1


@pytest.mark.parametrize("host_scope", [None, "capture-owner"], ids=["root-owner", "scoped-owner"])
@pytest.mark.asyncio
async def test_releasing_foreign_scope_preserves_canonical_capture_until_its_owner_releases(
    tmp_path: Path,
    host_scope: str | None,
) -> None:
    async with AsyncExitStack() as cleanup:
        run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        run.workspaces.set_default_patch("same profile candidate")
        host = await _candidate(run, "capture-owner") if host_scope is not None else None
        agent = await _candidate(run, "profile-agent")
        assert agent.id is not None
        canonical_scope = None
        if host is not None:
            canonical_scope = host.id
            assert canonical_scope is not None
        namespace = InMemoryEvaluationNamespace()
        executor = FakeEvaluationExecutor(
            clock=FakeClock(), supported_evidence_kinds=(EvidenceKind.PROFILE.value,)
        )
        backend = SemanticEvaluationBackend(
            run.evaluation, run.workspaces, namespace, _identity(), executor=executor
        )
        cleanup.push_async_callback(backend.close)
        backend.bind(AgentToolBindingContext(PROFILER, agent, "decode", str))
        service = EvaluationAgentService(backend, namespace, tmp_path / "profile.sock")
        cleanup.push_async_callback(service.close)
        revision = (
            await run.workspaces.root.snapshot("root profile") if host is None else host.revision
        )
        assert revision is not None
        host_submission = await backend.submit_revision_evidence(
            revision, (EvidenceKind.PROFILE,), scope_id=canonical_scope
        )
        grant = service.grant(
            principal_id="profile-agent", role=EvaluationAgentRole.PROFILER, scope_id=agent.id
        )
        reply = await service.dispatch(
            SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.PROFILE,))
        )
        assert isinstance(reply, SubmittedReply)
        assert reply.handle_id == host_submission.handle_id
        assert await service.scope_handles(agent.id) == (host_submission.handle_id,)
        if canonical_scope is not None:
            (pending,) = await service.settlements().observe(
                OwnedEvaluationDependencies(
                    scope_id=canonical_scope,
                    generation=0,
                    handles=(host_submission.handle_id,),
                )
            )
            assert pending.scope_id == canonical_scope

        foreign_release = await service.cancel_scope(agent.id)

        assert host_submission.handle_id not in foreign_release.evaluations
        assert await backend.recorded_status(host_submission.handle_id) not in {
            EvaluationState.CANCELED,
            EvaluationState.FAILED,
            EvaluationState.SUCCEEDED,
            EvaluationState.SUPERSEDED,
        }
        if canonical_scope is not None:
            canonical_release = await service.cancel_scope(canonical_scope)
            assert host_submission.handle_id in canonical_release.evaluations


@pytest.mark.parametrize("identity_field", ["evaluator", "workload", "environment"])
@pytest.mark.asyncio
async def test_profile_measurement_identity_fences_revision_plan_and_fingerprints(
    tmp_path: Path, identity_field: str
) -> None:
    async with AsyncExitStack() as cleanup:
        run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        first_revision = await run.workspaces.root.snapshot("first")
        second_revision = await run.workspaces.root.snapshot("second")
        run.workspaces.set_patch(first_revision, "first candidate")
        run.workspaces.set_patch(second_revision, "second candidate")
        namespace = InMemoryEvaluationNamespace()
        executor = FakeEvaluationExecutor(
            clock=FakeClock(), supported_evidence_kinds=(EvidenceKind.PROFILE.value,)
        )
        plan = TrustedEvaluationPlan(profile_command="capture-a", profile_timeout_seconds=60)

        def backend(
            identity: SemanticEvaluationIdentity | None = None,
            capture_plan: TrustedEvaluationPlan = plan,
        ) -> SemanticEvaluationBackend:
            instance = SemanticEvaluationBackend(
                run.evaluation,
                run.workspaces,
                namespace,
                _identity() if identity is None else identity,
                executor=executor,
                plan=capture_plan,
                queue_allowance_seconds=1,
                submitted_time=lambda: 1.0,
            )
            cleanup.push_async_callback(instance.close)
            return instance

        first = await backend().submit_revision_evidence(
            first_revision, (EvidenceKind.PROFILE,), scope_id="one"
        )
        changed_revision = await backend().submit_revision_evidence(
            second_revision, (EvidenceKind.PROFILE,), scope_id="two"
        )
        changed_plan = await backend(
            capture_plan=TrustedEvaluationPlan(
                profile_command="capture-b", profile_timeout_seconds=60
            )
        ).submit_revision_evidence(first_revision, (EvidenceKind.PROFILE,), scope_id="three")
        changed_fingerprint = await backend(
            _identity(**{identity_field: f"other {identity_field}"})
        ).submit_revision_evidence(first_revision, (EvidenceKind.PROFILE,), scope_id="four")

        assert (
            len(
                {
                    first.handle_id,
                    changed_revision.handle_id,
                    changed_plan.handle_id,
                    changed_fingerprint.handle_id,
                }
            )
            == 4
        )
        assert len(executor.submissions) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote_state",
    [EvaluationState.FAILED, EvaluationState.CANCELED, EvaluationState.SUPERSEDED],
)
async def test_unobserved_executor_failure_gets_a_fresh_shared_attempt(
    tmp_path: Path, remote_state: EvaluationState
) -> None:
    """Retry selection observes the executor before committing a requester join."""
    async with AsyncExitStack() as cleanup:
        run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        cleanup.push_async_callback(run.close)
        run.workspaces.set_default_patch("identical measured candidate")
        owner = await _candidate(run, "owner")
        requester = await _candidate(run, "requester")
        assert owner.revision is not None
        assert requester.revision is not None
        executor = FakeEvaluationExecutor(
            clock=FakeClock(), supported_evidence_kinds=(EvidenceKind.BENCHMARK.value,)
        )
        backend = SemanticEvaluationBackend(
            run.evaluation,
            run.workspaces,
            InMemoryEvaluationNamespace(),
            _identity(),
            executor=executor,
        )
        cleanup.push_async_callback(backend.close)
        original = await backend.submit_revision_evidence(
            owner.revision, (EvidenceKind.BENCHMARK,), scope_id=owner.id
        )
        executor.set_state(
            original.handle_id,
            remote_state,
            failure="executor lost its allocation"
            if remote_state is EvaluationState.FAILED
            else None,
        )
        assert await backend.recorded_status(original.handle_id) is EvaluationState.QUEUED

        fresh = await backend.submit_revision_evidence(
            requester.revision, (EvidenceKind.BENCHMARK,), scope_id=requester.id
        )

        assert fresh.handle_id != original.handle_id
        assert await backend.recorded_status(original.handle_id) is remote_state
        assert await backend.recorded_status(fresh.handle_id) is EvaluationState.QUEUED
        assert len(executor.submissions) == 2
        joined = await backend.submit_revision_evidence(
            owner.revision, (EvidenceKind.BENCHMARK,), scope_id=owner.id
        )
        assert joined.handle_id == fresh.handle_id
        assert len(executor.submissions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("passed", [True, False], ids=["passing-fallback", "failure-feedback"])
async def test_run_history_includes_a_joined_semantic_submission(
    tmp_path: Path, *, passed: bool
) -> None:
    """Real local semantic production feeds every submitting workspace's history."""
    async with AsyncExitStack() as cleanup:
        original = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        cleanup.push_async_callback(original.close)
        original.workspaces.set_default_patch("same submitted content")
        owner = await _candidate(original, "owner")
        requester = await _candidate(original, "requester")
        namespace = InMemoryEvaluationNamespace()
        backend = SemanticEvaluationBackend(
            original.evaluation, original.workspaces, namespace, _identity()
        )
        cleanup.push_async_callback(backend.close)
        for workspace in (owner, requester):
            backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, "history", str))
        service = EvaluationAgentService(backend, namespace, tmp_path / "history.sock")
        cleanup.push_async_callback(service.close)
        facade = EvidenceReusingEvaluation(
            original.evaluation, backend, run_id=original.run_id, scopes=service
        )
        run = Run(
            run_id=original.run_id,
            facts=original.facts,
            agents=original.agents,
            workspaces=original.workspaces,
            evaluation=facade,
            state=original.state,
            control=original.control,
            commands=original.commands,
            skills=original.skills,
            observations=original.observations,
        )
        original.evaluation.script_accuracy(
            AccuracyEvaluation(executed=True, feedback=None if passed else "accuracy mismatch")
        )
        submissions = []
        for workspace in (owner, requester):
            grant = service.grant(
                principal_id=str(workspace.id),
                role=EvaluationAgentRole.IMPLEMENTER,
                scope_id=workspace.id,
            )
            submitted = await service.dispatch(
                SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
            )
            assert isinstance(submitted, SubmittedReply)
            submissions.append(submitted.handle_id)
            assert isinstance(
                await backend.await_result(submitted.handle_id, 60), EvaluationCompleted
            )

        assert submissions[0] == submissions[1]
        assert len(original.evaluation.accuracy_calls) == 1
        (joined,) = await run.evaluation.agent_evaluations(requester)
        (canonical,) = await run.evaluation.agent_evaluations(owner)
        assert joined == canonical
        assert joined.status is (
            AgentEvaluationStatus.PASSED if passed else AgentEvaluationStatus.FAILED
        )
        assert joined.failure == (None if passed else "accuracy mismatch")


@pytest.mark.asyncio
@pytest.mark.parametrize("host_scope", ["same", "foreign", "root"])
async def test_joined_cancel_does_not_cancel_a_host_capture(
    tmp_path: Path, host_scope: str
) -> None:
    """A host wait exists before any agent joins, including within its own scope."""
    async with AsyncExitStack() as cleanup:
        run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        cleanup.push_async_callback(run.close)
        run.workspaces.set_default_patch("one physical measured candidate")
        requester = await _candidate(run, "requester")
        host = (
            requester
            if host_scope == "same"
            else (run.workspaces.root if host_scope == "root" else await _candidate(run, "host"))
        )
        host_revision = await host.snapshot("host capture")
        namespace = InMemoryEvaluationNamespace()
        backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
        cleanup.push_async_callback(backend.close)
        backend.bind(AgentToolBindingContext(IMPLEMENTER, requester, "join", str))
        service = EvaluationAgentService(backend, namespace, tmp_path / "host.sock")
        cleanup.push_async_callback(service.close)
        gate = run.evaluation.gate("accuracy", 0)
        submitted = await backend.submit_revision_evidence(
            host_revision, (EvidenceKind.ACCURACY,), scope_id=host.id
        )
        await gate.entered.wait()
        grant = service.grant(
            principal_id="requester", role=EvaluationAgentRole.IMPLEMENTER, scope_id=requester.id
        )
        joined = await service.dispatch(
            SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(joined, SubmittedReply)
        assert joined.handle_id == submitted.handle_id

        await service.dispatch(CancelCall(token=grant.token, handle_id=joined.handle_id))

        assert await backend.status(submitted.handle_id) is EvaluationState.RUNNING
        assert not gate.cancelled_while_live
        gate.release()
        assert isinstance(await backend.await_result(submitted.handle_id, 60), EvaluationCompleted)
        assert len(run.evaluation.accuracy_calls) == 1
        capture = await backend.recorded_snapshot(submitted.handle_id)
        assert capture.request.owner_scope == host.id
        assert capture.request.owner_generation == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [True, False], ids=["foreign-scope", "reopened-scope"])
async def test_scoped_facade_reads_requester_generation_and_canonical_report(
    tmp_path: Path, *, foreign: bool
) -> None:
    async with AsyncExitStack() as cleanup:
        run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        cleanup.push_async_callback(run.close)
        run.workspaces.set_default_patch("same immutable captured content")
        owner = await _candidate(run, "owner")
        requester = await _candidate(run, "requester") if foreign else owner
        assert owner.id is not None
        assert requester.id is not None
        namespace = InMemoryEvaluationNamespace()
        backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
        cleanup.push_async_callback(backend.close)
        for workspace in (owner, requester):
            backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, "scoped", str))
        service = EvaluationAgentService(backend, namespace, tmp_path / "scoped.sock")
        cleanup.push_async_callback(service.close)
        facade = EvidenceReusingEvaluation(
            run.evaluation, backend, run_id=run.run_id, scopes=service
        )
        owner_grant = service.grant(
            principal_id="owner", role=EvaluationAgentRole.IMPLEMENTER, scope_id=owner.id
        )
        original = await service.dispatch(
            SubmitCall(token=owner_grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(original, SubmittedReply)
        assert isinstance(await backend.await_result(original.handle_id, 60), EvaluationCompleted)
        await service.cancel_scope(requester.id)
        await service.reopen_scope(requester.id)
        requester_grant = service.grant(
            principal_id="requester", role=EvaluationAgentRole.IMPLEMENTER, scope_id=requester.id
        )
        joined = await service.dispatch(
            SubmitCall(token=requester_grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(joined, SubmittedReply)
        assert joined.handle_id == original.handle_id

        generation = await facade.submitted_generation(joined.handle_id, scope_id=requester.id)
        assert generation == 1
        assert (await backend.recorded_snapshot(joined.handle_id)).request.owner_generation == 0
        (observation,) = await facade.settlements().observe(
            OwnedEvaluationDependencies(
                scope_id=requester.id, generation=generation, handles=(joined.handle_id,)
            )
        )
        assert isinstance(observation.result, EvaluationCompleted)
        assert observation.scope_id == requester.id
        assert observation.generation == 1
        report = StoredEvaluation.model_validate_json(
            await facade.submitted_report(joined.handle_id, scope_id=requester.id)
        )
        assert report.request.owner_scope == owner.id
        assert report.request.owner_generation == 0
        assert (
            await facade.submitted_report(joined.handle_id, scope_id=owner.id)
            == report.model_dump_json()
        )
        assert len(run.evaluation.accuracy_calls) == 1


@pytest.mark.asyncio
async def test_requester_history_orders_joins_by_submission_instead_of_capture(
    tmp_path: Path,
) -> None:
    async with AsyncExitStack() as cleanup:
        original = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        cleanup.push_async_callback(original.close)
        owner = await _candidate(original, "owner")
        requester = await _candidate(original, "requester")
        namespace = InMemoryEvaluationNamespace()
        backend = SemanticEvaluationBackend(
            original.evaluation, original.workspaces, namespace, _identity()
        )
        cleanup.push_async_callback(backend.close)
        for workspace in (owner, requester):
            backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, "history", str))
        service = EvaluationAgentService(backend, namespace, tmp_path / "chronology.sock")
        cleanup.push_async_callback(service.close)
        facade = EvidenceReusingEvaluation(
            original.evaluation, backend, run_id=original.run_id, scopes=service
        )
        owner_grant = service.grant(
            principal_id="owner", role=EvaluationAgentRole.IMPLEMENTER, scope_id=owner.id
        )
        requester_grant = service.grant(
            principal_id="requester", role=EvaluationAgentRole.IMPLEMENTER, scope_id=requester.id
        )
        original.evaluation.script_accuracy(
            AccuracyEvaluation(executed=True),
            AccuracyEvaluation(executed=True, feedback="changed candidate fails"),
        )

        async def submit(token: str) -> str:
            reply = await service.dispatch(
                SubmitCall(token=token, evidence_kinds=(EvidenceKind.ACCURACY,))
            )
            assert isinstance(reply, SubmittedReply)
            assert isinstance(await backend.await_result(reply.handle_id, 60), EvaluationCompleted)
            return reply.handle_id

        original.workspaces.set_default_patch("original passing content")
        older_capture = await submit(owner_grant.token)
        original.workspaces.set_default_patch("changed failing content")
        newer_capture = await submit(requester_grant.token)
        original.workspaces.set_default_patch("original passing content")
        joined = await submit(requester_grant.token)

        assert joined == older_capture
        assert await service.scope_handles(requester.id) == (newer_capture, older_capture)
        outcomes = await facade.agent_evaluations(requester)
        assert [outcome.status for outcome in outcomes] == [
            AgentEvaluationStatus.FAILED,
            AgentEvaluationStatus.PASSED,
        ]
        assert len(original.evaluation.accuracy_calls) == 2
