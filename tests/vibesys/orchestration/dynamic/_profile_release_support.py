"""Compose real scope ownership with faithful profile-capable evaluator effects."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic._fault_boundaries import (
    Boundary,
    FaultBoundary,
    FaultNamespace,
    Side,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vs_evaluation.api import (
    ContentDigest,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationRequest,
    EvaluationState,
    EvaluationStateNamespace,
    EvidenceKind,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ScopeLifecycleStore,
    ScopePhase,
    SubmitCall,
    SubmittedReply,
    TrustedEvidence,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationExecutor,
    FakeProfilerTurnProvision,
    InMemoryEvaluationNamespace,
)
from vs_runtime.api import AgentToolBindingContext, Run

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api import CandidateWorkspace
    from vs_runtime.api.infrastructure import TrustedEvaluationPlan
    from vs_runtime.api.testing import FakeRun


class OwnedEvaluationExecutor(FakeEvaluationExecutor):
    """The evaluator Fake owns no process resources beyond its job ledger."""

    fault: FaultBoundary | None = None

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        if self.fault is not None:
            self.fault.hit(Boundary.SUBMIT, Side.BEFORE)
        await super().submit(request, handle_id=handle_id)
        if self.fault is not None:
            self.fault.hit(Boundary.SUBMIT, Side.AFTER)

    async def cancel(self, handle_id: str) -> None:
        if self.fault is not None:
            self.fault.hit(Boundary.CANCEL, Side.BEFORE)
        await super().cancel(handle_id)
        if self.fault is not None:
            self.fault.hit(Boundary.CANCEL, Side.AFTER)

    async def close(self) -> None:
        """Retain external jobs for deterministic crash-and-restart inspection."""


async def no_evidence(
    _principal: str,
    _scope: str | None,
    _snapshot: str,
    _ids: tuple[str, ...],
) -> tuple[TrustedEvidence, ...]:
    """No profiler agent has collected evidence while the capture is queued."""
    return ()


@dataclass
class ProfileReleaseEffects:
    """Real semantic/scope handlers with durable state and external Fake jobs."""

    base: FakeRun
    socket_path: Path
    executor: OwnedEvaluationExecutor
    namespace: EvaluationStateNamespace
    backend: SemanticEvaluationBackend
    service: EvaluationAgentService
    profiler: ProfilerAgentService
    evaluation: EvidenceReusingEvaluation
    run: Run

    async def own_resources(self, workspace: CandidateWorkspace, member: str) -> None:
        """Own a queued accuracy job and a trusted capture before caller cancellation."""
        self.backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, member, str))
        grant = self.service.grant(
            principal_id=f"implementer:{member}",
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=workspace.id,
        )
        submitted = await self.service.dispatch(
            SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(submitted, SubmittedReply)
        self.executor.wait_started.clear()
        profile = asyncio.create_task(
            self.evaluation.profile(
                await workspace.snapshot("capture"), "Profile decode", member_id=member
            )
        )
        await self.executor.wait_started.wait()
        profile.cancel()
        with suppress(asyncio.CancelledError):
            await profile
        assert {
            tuple(stage.name for stage in job.request.stages) for job in self.executor.submissions
        } == {
            (EvidenceKind.ACCURACY.value,),
            (EvidenceKind.PROFILE.value,),
        }

    def restart(self) -> Run:
        """Reload handler state while preserving the external executor job ledger."""
        self.service = EvaluationAgentService(
            self.backend,
            self.namespace,
            self.socket_path,
            self.profiler,
        )
        self.evaluation = EvidenceReusingEvaluation(
            self.base.evaluation,
            self.backend,
            run_id=self.base.run_id,
            scopes=self.service,
            profiler=self.profiler,
        )
        self.run = replace(self.run, evaluation=self.evaluation)
        return self.run

    async def assert_released(self, scope_id: str) -> None:
        """A completed release owns no surviving evaluation or trusted capture."""
        assert self.executor.backend.active_count == 0
        for job in self.executor.submissions:
            assert await self.backend.status(job.handle_id) is EvaluationState.CANCELED
        scope = next(
            scope
            for scope in ScopeLifecycleStore(self.namespace).snapshot().scopes
            if scope.scope_id == scope_id
        )
        assert scope.phase is ScopePhase.CLOSED

    async def close(self) -> None:
        """Drain all jobs before closing the semantic and profiler handlers."""
        await self.service.cancel_outstanding()
        await self.profiler.close()
        await self.backend.close()


def profile_release_effects(
    root: Path,
    base: FakeRun,
    *,
    fault: FaultBoundary | None = None,
    plan: TrustedEvaluationPlan | None = None,
) -> ProfileReleaseEffects:
    """Replace only the public Evaluation port when composing the run value."""
    namespace: EvaluationStateNamespace = InMemoryEvaluationNamespace()
    if fault is not None:
        namespace = FaultNamespace(namespace, fault)
    executor = OwnedEvaluationExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=tuple(kind.value for kind in EvidenceKind),
        advance_clock_on_timeout=False,
    )
    executor.fault = fault
    digest = ContentDigest.sha256(b"composed-trace")
    backend = SemanticEvaluationBackend(
        base.evaluation,
        base.workspaces,
        namespace,
        SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest),
        executor=executor,
        plan=plan,
        queue_allowance_seconds=900,
        submitted_time=executor.clock.monotonic,
    )
    profiler = ProfilerAgentService(
        FakeProfilerTurnProvision(),
        namespace,
        ProfilerAgentServiceHooks(partial(backend.snapshot, label="profile-trace"), no_evidence),
    )
    service = EvaluationAgentService(backend, namespace, root / "evaluation.sock", profiler)
    evaluation = EvidenceReusingEvaluation(
        base.evaluation, backend, run_id=base.run_id, scopes=service, profiler=profiler
    )
    run = Run(
        run_id=base.run_id,
        facts=base.facts,
        agents=base.agents,
        workspaces=base.workspaces,
        evaluation=evaluation,
        state=base.state,
        control=base.control,
        commands=base.commands,
        skills=base.skills,
        observations=base.observations,
    )
    return ProfileReleaseEffects(
        base=base,
        socket_path=root / "evaluation.sock",
        executor=executor,
        namespace=namespace,
        backend=backend,
        service=service,
        profiler=profiler,
        evaluation=evaluation,
        run=run,
    )
