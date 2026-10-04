"""Profiler termination keeps its evaluation dependency alive until acknowledgement."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from vibesys.run.host import close_evaluation_services
from vs_evaluation.api import (
    AvailabilitySnapshot,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvidenceCall,
    EvidenceKind,
    EvidenceReply,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerOperationState,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    RunStoppingReply,
    SubmitCall,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationExecutor,
    FakeProfilerTurnProvision,
    InMemoryEvaluationNamespace,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_evaluation.api import EvaluationBackend, ResourceRequirements, TrustedEvidence


class EmptyEvaluationBackend:
    """An evaluation namespace containing no submitted jobs or trusted results."""

    def __init__(self) -> None:
        self.executor = FakeEvaluationExecutor(FakeClock())

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        return await self.executor.availability(requirements)

    async def accepted_evidence(
        self, scope_id: str | None, kinds: tuple[EvidenceKind, ...]
    ) -> tuple[TrustedEvidence, ...]:
        del scope_id, kinds
        return ()

    async def drain_submissions(self, scope_id: str | None) -> None:
        del scope_id

    async def owned_handles(self, scope_id: str | None) -> tuple[str, ...]:
        del scope_id
        return ()


class SettlingProfilerProvision(FakeProfilerTurnProvision):
    """Hold cancellation acknowledgement while the profiler reads its dependency."""

    def __init__(self) -> None:
        super().__init__()
        self.evaluation: EvaluationAgentService | None = None
        self.cancel_entered = asyncio.Event()
        self.cancel_released = asyncio.Event()
        self.cancel_interruptions = 0

    async def cancel(self, operation_id: str) -> None:
        self.cancel_entered.set()
        assert self.evaluation is not None
        assert self.evaluation.socket_path.exists()
        grant = self.evaluation.grant(
            principal_id="profiler", role=EvaluationAgentRole.PROFILER, scope_id="candidate"
        )
        evidence = await self.evaluation.dispatch(
            EvidenceCall(token=grant.token, evidence_kinds=(EvidenceKind.PROFILE,))
        )
        assert isinstance(evidence, EvidenceReply)
        rejected = await self.evaluation.dispatch(
            SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.PROFILE,))
        )
        assert isinstance(rejected, RunStoppingReply)
        try:
            await self.cancel_released.wait()
        except asyncio.CancelledError:
            self.cancel_interruptions += 1
            raise
        await super().cancel(operation_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_count", [1, 3])
@pytest.mark.parametrize("cancel_shutdown", [False, True])
async def test_profiler_settlement_precedes_evaluation_close(
    tmp_path: Path, operation_count: int, *, cancel_shutdown: bool
) -> None:
    namespace = InMemoryEvaluationNamespace()
    provision = SettlingProfilerProvision()

    async def snapshot(scope: str | None) -> str:
        return scope or "root"

    async def evidence(
        principal: str, scope: str | None, candidate: str, evidence_ids: tuple[str, ...]
    ) -> tuple[TrustedEvidence, ...]:
        del principal, scope, candidate, evidence_ids
        return ()

    profilers = ProfilerAgentService(
        provision,
        namespace,
        ProfilerAgentServiceHooks(candidate_snapshot=snapshot, resolve_evidence=evidence),
    )
    evaluation = EvaluationAgentService(
        cast("EvaluationBackend", EmptyEvaluationBackend()),
        namespace,
        tmp_path / "evaluation.sock",
        profilers,
    )
    provision.evaluation = evaluation
    await evaluation.start()
    operations = []
    for index in range(operation_count):
        dispatched = await profilers.dispatch(
            principal_id="implementer",
            scope_id="candidate",
            request=f"Collect profile {index}",
            work=ProfilerWorkKey(
                purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC, focus=f"focus {index}"
            ),
            session_id=None,
        )
        operations.append(dispatched.operation_id)
        await provision.wait_started(dispatched.operation_id)

    closing = asyncio.create_task(close_evaluation_services(evaluation, profilers))
    await provision.cancel_entered.wait()
    assert evaluation.socket_path.exists()
    assert not closing.done()
    if cancel_shutdown:
        closing.cancel()
    provision.cancel_released.set()
    if cancel_shutdown:
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        assert await closing == []
    assert provision.cancel_interruptions == 0
    assert not evaluation.socket_path.exists()
    assert provision.canceled == operations
    for operation_id in operations:
        observed = await profilers.status(operation_id, "implementer", "candidate")
        assert observed.operation.state is ProfilerOperationState.INTERRUPTED
        assert observed.operation.result is None
