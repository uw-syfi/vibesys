"""Live profiler producer IDs never acquire evaluation continuation authority."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vibesys.run.evaluation_backend import SemanticEvaluationBackend, SemanticEvaluationIdentity
from vs_evaluation.api import (
    ContentDigest,
    EvaluationAgentAccessError,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationRequest,
    EvaluationStep,
    EvidenceFingerprints,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    TrustedEvidence,
    evaluation_principal,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements, FakeProfilerTurnProvision
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces


@pytest.mark.parametrize("implementation", ["service", "fake"])
@given(
    role=st.sampled_from(
        (EvaluationAgentRole.IMPLEMENTER, EvaluationAgentRole.JUDGE, EvaluationAgentRole.PROFILER)
    ),
    member=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
)
@example(role=EvaluationAgentRole.JUDGE, member="r23")
def test_live_own_and_foreign_profiler_ids_are_typed_wait_errors(
    implementation: Literal["service", "fake"], role: EvaluationAgentRole, member: str
) -> None:
    async def scenario() -> None:
        scope = "scope"
        principal = evaluation_principal(role, member, scope)
        settlements = FakeEvaluationSettlements()
        provision = FakeProfilerTurnProvision()
        workspace = FakeWorkspace(path=Path("/project"))

        async def snapshot(_scope: str | None) -> str:
            return await workspace.snapshot("profiler capture")

        async def evidence(
            _principal: str, _scope: str | None, _revision: str, ids: tuple[str, ...]
        ) -> tuple[TrustedEvidence, ...]:
            assert not ids
            return ()

        profiler = ProfilerAgentService(
            provision,
            settlements.namespace,
            ProfilerAgentServiceHooks(candidate_snapshot=snapshot, resolve_evidence=evidence),
        )
        digest = ContentDigest.sha256(b"captured evaluation")
        backend = SemanticEvaluationBackend(
            FakeEvaluation(),
            FakeWorkspaces(workspace),
            settlements.namespace,
            SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest),
        )
        service = EvaluationAgentService(
            backend, settlements.namespace, Path("unused.sock"), profiler_agents=profiler
        )
        evaluation = FakeEvaluation(settlement_observations=settlements)
        handle = await settlements.submit(
            EvaluationRequest(
                key="owned capture",
                owner_scope=scope,
                stages=(EvaluationStep(name="benchmark", payload={}),),
            ),
            EvidenceFingerprints(
                candidate=digest, evaluator=digest, workload=digest, environment=digest
            ),
            principal_id=principal,
        )
        operations = []
        try:
            for owner in (principal, f"{principal}:foreign"):
                dispatched = await profiler.dispatch(
                    principal_id=owner,
                    scope_id=scope,
                    request="Find the pending capture status.",
                    work=ProfilerWorkKey(
                        purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC, focus=owner
                    ),
                    session_id=None,
                )
                operations.append(dispatched.operation_id)
                await provision.wait_started(dispatched.operation_id)
            validate = (
                service.validate_wait if implementation == "service" else evaluation.validate_wait
            )
            for operation_id in operations:
                with pytest.raises(EvaluationAgentAccessError):
                    await validate((handle, operation_id), scope_id=scope, principal_id=principal)
            await validate((handle,), scope_id=scope, principal_id=principal)
            assert provision.active == set(operations)
            assert len(await settlements.submission_history(scope)) == 1
        finally:
            await profiler.close()

    asyncio.run(scenario())
