"""Historical requester report access survives withdrawal from shared captures."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.run.test_evaluation_measurement_identity import _candidate, _identity

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.run.evaluation_backend import EvidenceReusingEvaluation, SemanticEvaluationBackend
from vs_evaluation.api import (
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationCompleted,
    EvaluationDependencyError,
    EvaluationState,
    EvidenceKind,
    OwnedEvaluationDependencies,
    StoredEvaluation,
    SubmitCall,
    SubmittedReply,
)
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_runtime.api import AgentToolBindingContext
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_scope", [False, True], ids=["cancel-association", "release-scope"]
)
async def test_withdrawn_requester_keeps_historical_report_access(
    tmp_path: Path, *, release_scope: bool
) -> None:
    async with AsyncExitStack() as cleanup:
        run = FakeRun(PLUGIN, project_root=tmp_path, supports_parallel_candidates=True)
        cleanup.push_async_callback(run.close)
        run.workspaces.set_default_patch("one shared captured candidate")
        owner = await _candidate(run, "owner")
        requester = await _candidate(run, "requester")
        assert owner.id is not None
        assert requester.id is not None
        namespace = InMemoryEvaluationNamespace()
        backend = SemanticEvaluationBackend(run.evaluation, run.workspaces, namespace, _identity())
        cleanup.push_async_callback(backend.close)
        for workspace in (owner, requester):
            backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, "history", str))
        service = EvaluationAgentService(backend, namespace, tmp_path / "history.sock")
        cleanup.push_async_callback(service.close)
        evaluation = EvidenceReusingEvaluation(
            run.evaluation, backend, run_id=run.run_id, scopes=service
        )
        gate = run.evaluation.gate("accuracy", 0)
        owner_grant = service.grant(
            principal_id="owner", role=EvaluationAgentRole.IMPLEMENTER, scope_id=owner.id
        )
        original = await service.dispatch(
            SubmitCall(token=owner_grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(original, SubmittedReply)
        await gate.entered.wait()
        requester_grant = service.grant(
            principal_id="requester", role=EvaluationAgentRole.IMPLEMENTER, scope_id=requester.id
        )
        joined = await service.dispatch(
            SubmitCall(token=requester_grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(joined, SubmittedReply)
        assert joined.handle_id == original.handle_id
        assert await evaluation.submitted_generation(joined.handle_id, scope_id=requester.id) == 0

        with pytest.raises(EvaluationDependencyError):
            await evaluation.cancel_submitted(joined.handle_id, scope_id="unrelated-workspace")
        if release_scope:
            await evaluation.release_jobs("requester")
        else:
            await evaluation.cancel_submitted(joined.handle_id, scope_id=requester.id)
            await evaluation.cancel_submitted(joined.handle_id, scope_id=requester.id)

        assert await evaluation.submitted_generation(joined.handle_id, scope_id=requester.id) == 0
        with pytest.raises(EvaluationDependencyError):
            await evaluation.settlements().observe(
                OwnedEvaluationDependencies(
                    scope_id=requester.id, generation=0, handles=(joined.handle_id,)
                )
            )
        report = StoredEvaluation.model_validate_json(
            await evaluation.submitted_report(joined.handle_id, scope_id=requester.id)
        )
        assert report.request.owner_scope == owner.id
        assert report.state is EvaluationState.RUNNING
        assert not gate.cancelled_while_live
        with pytest.raises(EvaluationDependencyError):
            await evaluation.submitted_report(joined.handle_id, scope_id="unrelated-workspace")
        assert await evaluation.submitted_generation(joined.handle_id, scope_id=owner.id) == 0
        gate.release()
        assert isinstance(await backend.await_result(joined.handle_id, 60), EvaluationCompleted)
        settled = StoredEvaluation.model_validate_json(
            await evaluation.submitted_report(joined.handle_id, scope_id=requester.id)
        )
        assert settled.state is EvaluationState.SUCCEEDED
        assert len(run.evaluation.accuracy_calls) == 1
        if release_scope:
            await _assert_reopened_generation(evaluation, service, requester.id, joined.handle_id)


async def _assert_reopened_generation(
    evaluation: EvidenceReusingEvaluation,
    service: EvaluationAgentService,
    scope_id: str,
    handle_id: str,
) -> None:
    await service.reopen_scope(scope_id)
    assert await evaluation.submitted_generation(handle_id, scope_id=scope_id) == 0
    grant = service.grant(
        principal_id="requester", role=EvaluationAgentRole.IMPLEMENTER, scope_id=scope_id
    )
    joined = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(joined, SubmittedReply)
    assert joined.handle_id == handle_id
    assert await evaluation.submitted_generation(handle_id, scope_id=scope_id) == 1
    with pytest.raises(EvaluationDependencyError):
        await evaluation.settlements().observe(
            OwnedEvaluationDependencies(scope_id=scope_id, generation=0, handles=(handle_id,))
        )
