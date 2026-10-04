"""Measured evaluation identity across requester scopes."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, PROFILER
from vibesys.run.evaluation_backend import (
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vs_evaluation.api import (
    ContentDigest,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationState,
    EvidenceKind,
    OwnedEvaluationDependencies,
    SubmitCall,
    SubmittedReply,
)
from vs_evaluation.api.testing import FakeClock, FakeEvaluationExecutor, InMemoryEvaluationNamespace
from vs_runtime.api import AgentRole, AgentToolBindingContext, CandidateWorkspace
from vs_runtime.api.infrastructure import TrustedEvaluationPlan
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _OwnedFakeExecutor(FakeEvaluationExecutor):
    """Satisfy the semantic executor's owned cleanup contract."""

    async def close(self) -> None:
        """Cancel every accepted nonterminal execution before releasing ownership."""
        terminal = {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
            EvaluationState.CANCELED,
            EvaluationState.SUPERSEDED,
        }
        for submission in self.submissions:
            observation = await self.inspect(submission.handle_id)
            if observation is not None and observation.state not in terminal:
                await self.cancel(submission.handle_id)


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
        executor = _OwnedFakeExecutor(
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
        executor = _OwnedFakeExecutor(
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
        assert await service.scope_handles(agent.id) == ()
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
        executor = _OwnedFakeExecutor(
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
