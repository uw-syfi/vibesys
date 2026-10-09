"""Public contract tests for role-limited asynchronous evaluation access."""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from agentshim.testing import FakeExecutor, FakeRun, installed_mcp_servers, scripted_turn
from hypothesis import given
from hypothesis import strategies as st
from tests.support.evaluation_scenarios import ScenarioSpec, build_scenario, capture_submission

from vibesys.run.evaluation_backend import SemanticEvaluationStage
from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentInvocationState,
    AgentSessionKey,
    AgentSessionSpec,
    AgentSessionState,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    DurableSessionStore,
    MCPServerSpec,
    SessionScope,
)
from vs_agent.api.testing import fake_agentshim_driver
from vs_evaluation.api import (
    MAX_AGENT_AWAIT_S,
    AvailabilityCall,
    AvailabilityReply,
    AvailabilitySnapshot,
    AvailabilityState,
    AwaitCall,
    AwaitProfilerCall,
    AwaitReply,
    CancelCall,
    CanceledReply,
    CancelProfilerCall,
    CostClass,
    DispatchProfilerCall,
    EvaluationAgentAccessError,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationAgentSocketError,
    EvaluationAwaitResult,
    EvaluationCompleted,
    EvaluationCoordinator,
    EvaluationGrant,
    EvaluationOperationSnapshot,
    EvaluationState,
    EvaluationStepResult,
    EvaluationStillRunning,
    EvidenceCall,
    EvidenceKind,
    EvidenceReply,
    ExecutorObservation,
    FilesystemEvaluationStore,
    ProfileField,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerStatusCall,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    ResourceRequirements,
    ReuseStatus,
    RunOperationsCall,
    RunOperationsReply,
    RunStoppingReply,
    ScopeSubmissionTracker,
    StatusCall,
    StatusReply,
    StoredEvaluation,
    SubmitCall,
    SubmittedReply,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationBackend,
    FakeEvaluationExecutor,
    FakeProfilerTurnProvision,
    InMemoryEvaluationStore,
)
from vs_evaluation.api.tools import build_evaluation_tools, evaluation_mcp_descriptor
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
    StateNamespace,
)
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import agentshim
    from pydantic import BaseModel


class _SemanticBackend:
    """Faithful semantic facade Fake over the provider-neutral coordinator."""

    def __init__(self, coordinator: EvaluationCoordinator) -> None:
        self._coordinator = coordinator
        self._submissions = ScopeSubmissionTracker()

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        return await self._coordinator.availability(requirements)

    async def submit_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
        *,
        own: Callable[[SubmittedSemanticEvaluation], Awaitable[None]],
        required_profile_fields: tuple[ProfileField, ...] = (),
    ) -> SubmittedSemanticEvaluation:
        async with self._submissions.track(scope_id):
            content = scope_id or "root"
            request, submitted = await capture_submission(
                ScenarioSpec(
                    revision=content,
                    patch=content,
                    scope_id=scope_id,
                    kinds=kinds,
                    required_profile_fields=required_profile_fields,
                )
            )
            await self._coordinator.prepare(request)
            await own(submitted)
            self._submissions.check_admission()
            handle = await self._coordinator.submit(request)
            assert handle.id == submitted.handle_id
            return submitted

    async def drain_submissions(self, scope_id: str | None) -> None:
        """Join any submission admitted before closure."""
        await self._submissions.drain(scope_id)

    async def accepted_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
    ) -> tuple[TrustedEvidence, ...]:
        del scope_id, kinds
        return ()

    async def owned_handles(self, scope_id: str | None) -> tuple[str, ...]:
        """Read the scope identity durably attached to each claimed request."""
        return tuple(
            record.handle_id
            for record in await self._coordinator.history()
            if scope_id is None or record.request.owner_scope == scope_id
        )

    async def inspect_snapshot(self, handle_id: str) -> StoredEvaluation | None:
        """Inspect once without starting or cancelling external work."""
        return await self._coordinator.inspect_snapshot(handle_id)

    async def recorded_snapshot(self, handle_id: str) -> StoredEvaluation:
        return await self._coordinator.recorded_snapshot(handle_id)

    async def recorded_submission(self, handle_id: str) -> SubmittedSemanticEvaluation | None:
        record = await self._coordinator.recorded_snapshot(handle_id)
        payload = record.request.stages[0].payload
        if not isinstance(payload, dict) or "fingerprints" not in payload:
            return None
        # These fixture revision labels are their original patch text, so a
        # restart replays the immutable capture through the real producer.
        capture = SemanticEvaluationStage.model_validate(payload)
        _, submitted = await capture_submission(
            ScenarioSpec(
                revision=capture.snapshot,
                patch=capture.snapshot,
                scope_id=record.request.owner_scope,
                kinds=tuple(EvidenceKind(stage.name) for stage in record.request.stages),
            )
        )
        assert submitted.handle_id == record.handle_id
        assert submitted.fingerprints == capture.fingerprints
        return submitted

    async def recorded_status(self, handle_id: str) -> EvaluationState:
        """Read committed state without dispatching work."""
        return await self._coordinator.recorded_status(handle_id)

    async def status(self, handle_id: str) -> EvaluationState:
        return await self._coordinator.status(handle_id)

    async def operation_snapshot(self, handle_id: str) -> EvaluationOperationSnapshot:
        record = await self._coordinator.snapshot(handle_id)
        return EvaluationOperationSnapshot(
            handle_id=handle_id,
            candidate_revision=SemanticEvaluationStage.model_validate(
                record.request.stages[0].payload
            ).snapshot,
            state=record.state,
            current_stage=record.current_stage,
            evidence_recorded=False,
        )

    async def recorded_operation_snapshot(self, handle_id: str) -> EvaluationOperationSnapshot:
        record = await self._coordinator.recorded_snapshot(handle_id)
        return EvaluationOperationSnapshot(
            handle_id=handle_id,
            candidate_revision=SemanticEvaluationStage.model_validate(
                record.request.stages[0].payload
            ).snapshot,
            state=record.state,
            current_stage=record.current_stage,
            evidence_recorded=False,
        )

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        return await self._coordinator.await_result(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> StoredEvaluation:
        return await self._coordinator.cancel(handle_id)


class _BlockingSemanticBackend(_SemanticBackend):
    """Await port that blocks on an explicit event until canceled or released."""

    def __init__(self, coordinator: EvaluationCoordinator) -> None:
        super().__init__(coordinator)
        self.await_started = asyncio.Event()
        self.release_await = asyncio.Event()

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        self.await_started.set()
        await self.release_await.wait()
        return await super().await_result(handle_id, timeout_s)


class _BlockingAvailabilityBackend(_SemanticBackend):
    """Availability port released only after its socket peer disconnects."""

    def __init__(self, coordinator: EvaluationCoordinator) -> None:
        super().__init__(coordinator)
        self.availability_started = asyncio.Event()
        self.release_availability = asyncio.Event()

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        self.availability_started.set()
        await self.release_availability.wait()
        return await super().availability(requirements)


def _namespace(tmp_path: Path) -> StateNamespace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run_id = "evaluation-agent-test"
    if project.state.current_run_id() == run_id:
        return project.state.local_namespace(run_id, "evaluation-agent")
    manifest = project.state.new_run_manifest(
        "Evaluation agent test",
        run_id=run_id,
        trusted_input_baseline="a" * 40,
        branch="test/evaluation-agent",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="test-model",
            agent_backend="stub",
            compute_backend="cpu",
            requested_profiler="none",
            resolved_profiler="none",
            agent_roles={},
        ),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    return project.state.local_namespace(manifest.run_id, "evaluation-agent")


def _service(
    tmp_path: Path,
) -> tuple[EvaluationAgentService, FakeEvaluationExecutor]:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(
        clock,
        supported_evidence_kinds=("accuracy", "benchmark", "profile"),
    )
    coordinator = EvaluationCoordinator(
        executor,
        InMemoryEvaluationStore(),
        clock,
        max_await_timeout_s=20,
    )
    return (
        EvaluationAgentService(
            _SemanticBackend(coordinator),
            _namespace(tmp_path),
            tmp_path / "evaluation.sock",
        ),
        executor,
    )


@pytest.mark.asyncio
async def test_submit_returns_without_completion_and_timeout_does_not_cancel(
    tmp_path: Path,
) -> None:
    service, executor = _service(tmp_path)
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )

    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    awaited = await service.dispatch(
        AwaitCall(token=grant.token, handle_id=submitted.handle_id, timeout_s=3)
    )

    assert isinstance(awaited, AwaitReply)
    assert isinstance(awaited.result, EvaluationStillRunning)
    observation = await executor.inspect(submitted.handle_id)
    assert observation is not None
    assert observation.state is EvaluationState.QUEUED


@pytest.mark.asyncio
async def test_roles_enforce_semantic_kinds_and_judge_reads_only_trusted_evidence(
    tmp_path: Path,
) -> None:
    service, _executor = _service(tmp_path)
    profiler = service.grant(
        principal_id="profiler-1",
        role=EvaluationAgentRole.PROFILER,
        scope_id=None,
    )
    judge = service.grant(
        principal_id="judge-1",
        role=EvaluationAgentRole.JUDGE,
        scope_id=None,
    )

    with pytest.raises(EvaluationAgentAccessError, match=r"cannot request|role cannot request"):
        await service.dispatch(
            SubmitCall(token=profiler.token, evidence_kinds=(EvidenceKind.BENCHMARK,))
        )
    with pytest.raises(EvaluationAgentAccessError, match="read accepted evidence only"):
        await service.dispatch(AvailabilityCall(token=judge.token, evidence_kinds=()))
    evidence = await service.dispatch(EvidenceCall(token=judge.token))
    profiler_evidence = await service.dispatch(
        EvidenceCall(token=profiler.token, evidence_kinds=(EvidenceKind.PROFILE,))
    )

    assert isinstance(evidence, EvidenceReply)
    assert evidence.evidence == ()
    assert isinstance(profiler_evidence, EvidenceReply)
    assert profiler_evidence.evidence == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_id", [None, "candidate", "other-candidate"])
@pytest.mark.parametrize("call_type", [StatusCall, AwaitCall, CancelCall])
async def test_judge_can_observe_same_scope_status_without_wait_or_mutation(
    tmp_path: Path,
    scope_id: str | None,
    call_type: type[StatusCall | AwaitCall | CancelCall],
) -> None:
    service, executor = _service(tmp_path)
    owner = service.grant(
        principal_id="implementer",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate",
    )
    judge = service.grant(
        principal_id="judge",
        role=EvaluationAgentRole.JUDGE,
        scope_id=scope_id,
    )
    submitted = await service.dispatch(
        SubmitCall(token=owner.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    before = await executor.inspect(submitted.handle_id)
    call = (
        AwaitCall(token=judge.token, handle_id=submitted.handle_id, timeout_s=1)
        if call_type is AwaitCall
        else call_type.model_validate({"token": judge.token, "handle_id": submitted.handle_id})
    )
    if call_type is StatusCall and scope_id == "candidate":
        assert isinstance(await service.dispatch(call), StatusReply)
    else:
        with pytest.raises(EvaluationAgentAccessError):
            await service.dispatch(call)
    assert await executor.inspect(submitted.handle_id) == before
    assert isinstance(await service.dispatch(EvidenceCall(token=judge.token)), EvidenceReply)


@pytest.mark.asyncio
async def test_kind_the_executor_cannot_produce_is_rejected_without_a_handle(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, supported_evidence_kinds=("accuracy", "benchmark"))
    store = InMemoryEvaluationStore()
    service = EvaluationAgentService(
        _SemanticBackend(EvaluationCoordinator(executor, store, clock, max_await_timeout_s=20)),
        _namespace(tmp_path),
        tmp_path / "evaluation.sock",
    )
    profiler = service.grant(
        principal_id="profiler-1",
        role=EvaluationAgentRole.PROFILER,
        scope_id=None,
    )

    with pytest.raises(EvaluationAgentAccessError, match="cannot produce evidence kind: profile"):
        await service.dispatch(
            SubmitCall(token=profiler.token, evidence_kinds=(EvidenceKind.PROFILE,))
        )

    assert await store.records() == ()
    assert executor.submissions == []


@pytest.mark.asyncio
async def test_implementer_reads_profile_evidence_but_cannot_submit_it(tmp_path: Path) -> None:
    service, _executor = _service(tmp_path)
    implementer = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )

    with pytest.raises(EvaluationAgentAccessError, match=r"cannot request|role cannot request"):
        await service.dispatch(
            SubmitCall(token=implementer.token, evidence_kinds=(EvidenceKind.PROFILE,))
        )
    evidence = await service.dispatch(
        EvidenceCall(token=implementer.token, evidence_kinds=(EvidenceKind.PROFILE,))
    )
    assert isinstance(evidence, EvidenceReply)
    assert evidence.evidence == ()


@pytest.mark.asyncio
async def test_portfolio_dispatch_can_inspect_availability_but_cannot_submit(
    tmp_path: Path,
) -> None:
    service, _executor = _service(tmp_path)
    observer = service.grant(
        principal_id="portfolio-dispatch",
        role=EvaluationAgentRole.PORTFOLIO_DISPATCH,
        scope_id=None,
    )

    available = await service.dispatch(AvailabilityCall(token=observer.token))

    assert isinstance(available, AvailabilityReply)
    with pytest.raises(EvaluationAgentAccessError, match="availability only"):
        await service.dispatch(
            SubmitCall(token=observer.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )


@pytest.mark.asyncio
async def test_orchestrator_observes_profile_availability_without_submit_authority(
    tmp_path: Path,
) -> None:
    service, _executor = _service(tmp_path)
    orchestrator = service.grant(
        principal_id="orchestrator",
        role=EvaluationAgentRole.ORCHESTRATOR,
        scope_id=None,
    )
    available = await service.dispatch(
        AvailabilityCall(
            token=orchestrator.token,
            evidence_kinds=(EvidenceKind.PROFILE,),
        )
    )

    assert isinstance(available, AvailabilityReply)
    assert available.snapshot.supported_evidence_kinds == (EvidenceKind.PROFILE.value,)
    tools = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token=orchestrator.token,
        role=EvaluationAgentRole.ORCHESTRATOR,
    )
    availability_tool = next(tool for tool in tools if tool.name == "evaluation_availability")
    assert "kinds this role may not submit" in availability_tool.description
    with pytest.raises(EvaluationAgentAccessError, match="role cannot request evidence kind"):
        await service.dispatch(
            SubmitCall(
                token=orchestrator.token,
                evidence_kinds=(EvidenceKind.PROFILE,),
            )
        )


@pytest.mark.asyncio
async def test_run_observer_reads_all_evaluations_without_widening_other_roles(
    tmp_path: Path,
) -> None:
    service, _executor = _service(tmp_path)
    implementer = service.grant(
        principal_id="implementer:hypothesis-attention",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate-attention",
    )
    observer = service.grant(
        principal_id="orchestrator:root",
        role=EvaluationAgentRole.RUN_OBSERVER,
        scope_id="root",
        run_observer=True,
    )
    ordinary_orchestrator = service.grant(
        principal_id="orchestrator:legacy",
        role=EvaluationAgentRole.ORCHESTRATOR,
        scope_id="root",
    )
    submitted = await service.dispatch(
        SubmitCall(token=implementer.token, evidence_kinds=(EvidenceKind.BENCHMARK,))
    )
    assert isinstance(submitted, SubmittedReply)

    observed = await service.dispatch(RunOperationsCall(token=observer.token))

    assert isinstance(observed, RunOperationsReply)
    assert len(observed.evaluations) == 1
    operation = observed.evaluations[0]
    assert operation.principal_ids == ("implementer:hypothesis-attention",)
    assert operation.scope_id == "candidate-attention"
    assert operation.evidence_kinds == (EvidenceKind.BENCHMARK,)
    assert operation.state is EvaluationState.QUEUED
    assert not operation.evidence_recorded
    assert len(operation.candidate_content_digest) == 64
    with pytest.raises(EvaluationAgentAccessError, match="run-wide trusted operations"):
        await service.dispatch(RunOperationsCall(token=ordinary_orchestrator.token))
    with pytest.raises(EvaluationAgentAccessError, match="run-wide trusted operations"):
        await service.dispatch(RunOperationsCall(token=implementer.token))

    observer_tools = build_evaluation_tools(
        socket_path=tmp_path / "unused.sock",
        token=observer.token,
        role=EvaluationAgentRole.RUN_OBSERVER,
        run_observer=True,
    )
    ordinary_tools = build_evaluation_tools(
        socket_path=tmp_path / "unused.sock",
        token=ordinary_orchestrator.token,
        role=EvaluationAgentRole.ORCHESTRATOR,
    )
    assert "trusted_operations" in {tool.name for tool in observer_tools}
    assert "trusted_operations" not in {tool.name for tool in ordinary_tools}
    assert {tool.name for tool in observer_tools} == {
        "evaluation_availability",
        "trusted_operations",
    }
    for call in (
        SubmitCall(token=observer.token, evidence_kinds=(EvidenceKind.BENCHMARK,)),
        StatusCall(token=observer.token, handle_id=submitted.handle_id),
        AwaitCall(token=observer.token, handle_id=submitted.handle_id, timeout_s=1),
        CancelCall(token=observer.token, handle_id=submitted.handle_id),
        EvidenceCall(token=observer.token),
    ):
        with pytest.raises(EvaluationAgentAccessError):
            await service.dispatch(call)


def test_implementer_profiler_tool_discourages_duplicate_work(tmp_path: Path) -> None:
    tools = build_evaluation_tools(
        socket_path=tmp_path / "unused.sock",
        token="x" * 32,
        role=EvaluationAgentRole.IMPLEMENTER,
        profiler_available=True,
    )
    dispatch = next(tool for tool in tools if tool.name == "dispatch_profiler")
    assert "do not repeat an identical request" in dispatch.description


@pytest.mark.asyncio
async def test_handle_is_visible_within_scope_but_not_to_an_unrelated_scope(
    tmp_path: Path,
) -> None:
    service, _executor = _service(tmp_path)
    first = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    second = service.grant(
        principal_id="implementer-2",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    unrelated = service.grant(
        principal_id="implementer-3",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="other",
    )

    one = await service.dispatch(
        SubmitCall(token=first.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(one, SubmittedReply)
    await service.dispatch(StatusCall(token=second.token, handle_id=one.handle_id))
    with pytest.raises(EvaluationAgentAccessError, match="not visible"):
        await service.dispatch(StatusCall(token=unrelated.token, handle_id=one.handle_id))


@pytest.mark.asyncio
async def test_only_a_submitting_requester_can_cancel_its_association(tmp_path: Path) -> None:
    service, _executor = _service(tmp_path)
    owner = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    orchestrator = service.grant(
        principal_id="orchestrator",
        role=EvaluationAgentRole.ORCHESTRATOR,
        scope_id=None,
    )
    observer = service.grant(
        principal_id="implementer-2",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    submitted = await service.dispatch(
        SubmitCall(token=owner.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)

    await service.dispatch(StatusCall(token=observer.token, handle_id=submitted.handle_id))
    for grant in (observer, orchestrator):
        with pytest.raises(EvaluationAgentAccessError, match="only a submitting requester"):
            await service.dispatch(CancelCall(token=grant.token, handle_id=submitted.handle_id))

    canceled = await service.dispatch(CancelCall(token=owner.token, handle_id=submitted.handle_id))
    assert isinstance(canceled, CanceledReply)
    assert canceled.status is EvaluationState.CANCELED


def test_mcp_descriptor_carries_only_private_service_grant(tmp_path: Path) -> None:
    service, _executor = _service(tmp_path)
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )

    descriptor = evaluation_mcp_descriptor(grant, str(service.socket_path))

    assert descriptor.args == ("-m", "vs_evaluation.agent_mcp")
    environment = {**dict(descriptor.env), **dict(descriptor.runtime_env)}
    assert environment["VS_EVALUATION_ROLE"] == "implementer"
    assert environment["VS_EVALUATION_TOKEN"] == grant.token
    assert environment["VS_EVALUATION_PROFILER_AVAILABLE"] == "0"
    assert "slurm" not in repr(descriptor).lower()


def test_grant_is_stable_for_one_principal_role_and_scope(tmp_path: Path) -> None:
    service, _executor = _service(tmp_path)

    first = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate-1",
    )
    second = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate-1",
    )

    assert second is first


def test_mcp_tools_are_role_scoped_without_provider_commands(tmp_path: Path) -> None:
    implementer = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token="x" * 32,
        role=EvaluationAgentRole.IMPLEMENTER,
        profiler_available=True,
    )
    judge = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token="x" * 32,
        role=EvaluationAgentRole.JUDGE,
    )
    profiler = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token="x" * 32,
        role=EvaluationAgentRole.PROFILER,
    )
    portfolio_dispatch = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token="x" * 32,
        role=EvaluationAgentRole.PORTFOLIO_DISPATCH,
    )
    run_observer = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token="x" * 32,
        role=EvaluationAgentRole.RUN_OBSERVER,
        run_observer=True,
    )

    assert {tool.name for tool in implementer} == {
        "evaluation_availability",
        "submit_evaluation",
        "evaluation_status",
        "await_evaluation",
        "cancel_evaluation",
        "accepted_evidence",
        "dispatch_profiler",
        "profiler_status",
        "await_profiler",
        "cancel_profiler",
        "profiler_operations",
    }
    assert {tool.name for tool in judge} == {"accepted_evidence", "evaluation_status"}
    assert {tool.name for tool in profiler} == {
        "evaluation_availability",
        "submit_evaluation",
        "evaluation_status",
        "await_evaluation",
        "cancel_evaluation",
        "accepted_evidence",
    }
    assert {tool.name for tool in portfolio_dispatch} == {"evaluation_availability"}
    assert {tool.name for tool in run_observer} == {
        "evaluation_availability",
        "trusted_operations",
    }
    assert "command" not in " ".join(
        tool.description
        for tool in (*implementer, *profiler, *judge, *portfolio_dispatch, *run_observer)
    )


def test_implementer_profiler_surface_is_absent_without_a_provision(tmp_path: Path) -> None:
    tools = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token="x" * 32,
        role=EvaluationAgentRole.IMPLEMENTER,
        profiler_available=False,
    )
    names = {tool.name for tool in tools}

    assert not names.intersection(
        {"dispatch_profiler", "profiler_status", "await_profiler", "cancel_profiler"}
    )


@pytest.mark.asyncio
async def test_run_owned_socket_rejects_unknown_fields_and_live_path_reuse(tmp_path: Path) -> None:
    service, _executor = _service(tmp_path)
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    await service.start()
    second, _ = _service(tmp_path)
    with pytest.raises(EvaluationAgentSocketError, match="already listening"):
        await second.start()

    reader, writer = await asyncio.open_unix_connection(service.socket_path)
    writer.write(
        json.dumps(
            {
                "action": "status",
                "token": grant.token,
                "handle_id": "opaque",
                "provider_command": "forbidden",
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    await service.close()

    assert response["ok"] is False
    assert "provider_command" in response["error"]
    assert not service.socket_path.exists()


@pytest.mark.asyncio
async def test_concurrent_resume_has_one_socket_owner_and_loser_cannot_unlink_it(
    tmp_path: Path,
) -> None:
    first, _ = _service(tmp_path)
    second, _ = _service(tmp_path)
    outcomes = await asyncio.gather(first.start(), second.start(), return_exceptions=True)
    winner = first if outcomes[0] is None else second
    loser = second if winner is first else first

    assert sum(result is None for result in outcomes) == 1
    assert sum(isinstance(result, EvaluationAgentSocketError) for result in outcomes) == 1
    await loser.close()
    reader, writer = await asyncio.open_unix_connection(winner.socket_path)
    writer.close()
    await writer.wait_closed()
    del reader
    await winner.close()

    assert not winner.socket_path.exists()


@pytest.mark.parametrize(
    "role",
    [
        EvaluationAgentRole.PROFILER,
        EvaluationAgentRole.JUDGE,
        EvaluationAgentRole.ORCHESTRATOR,
        EvaluationAgentRole.PORTFOLIO_DISPATCH,
        EvaluationAgentRole.RUN_OBSERVER,
    ],
)
@pytest.mark.parametrize(
    "call_factory",
    [
        lambda token: DispatchProfilerCall(
            token=token,
            work=ProfilerWorkKey(
                purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC,
                focus="one gap",
            ),
            request="Inspect one gap.",
        ),
        lambda token: ProfilerStatusCall(token=token, operation_id="operation"),
        lambda token: AwaitProfilerCall(token=token, operation_id="operation", timeout_s=1),
        lambda token: CancelProfilerCall(token=token, operation_id="operation"),
    ],
)
@pytest.mark.asyncio
async def test_socket_rejects_profiler_lifecycle_for_nonimplementer_roles(
    tmp_path: Path,
    role: EvaluationAgentRole,
    call_factory: Callable[[str], BaseModel],
) -> None:
    service, _executor = _service(tmp_path)
    grant = service.grant(principal_id=role.value, role=role, scope_id=None)
    await service.start()
    try:
        reader, writer = await asyncio.open_unix_connection(service.socket_path)
        call = call_factory(grant.token)
        writer.write(call.model_dump_json().encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        await service.close()

    assert response["ok"] is False
    assert "implementers only" in response["error"]


@pytest.mark.asyncio
async def test_availability_tool_round_trips_strict_enums_over_socket(tmp_path: Path) -> None:
    service, _executor = _service(tmp_path)
    grant = service.grant(
        principal_id="portfolio-dispatch",
        role=EvaluationAgentRole.PORTFOLIO_DISPATCH,
        scope_id=None,
    )
    await service.start()
    try:
        (tool,) = build_evaluation_tools(
            socket_path=service.socket_path,
            token=grant.token,
            role=EvaluationAgentRole.PORTFOLIO_DISPATCH,
        )
        raw = await asyncio.to_thread(tool.handler, tool.input_schema())
    finally:
        await service.close()

    reply = AvailabilityReply.model_validate_json(raw)
    assert reply.snapshot.state is AvailabilityState.IMMEDIATE
    assert reply.snapshot.reuse_status is ReuseStatus.NONE
    assert reply.snapshot.cost_class is CostClass.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_s", [MAX_AGENT_AWAIT_S + 1, 900.0, 1800.0])
async def test_await_tool_states_its_cap_and_waits_the_cap_for_longer_requests(
    tmp_path: Path, requested_s: float
) -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, supported_evidence_kinds=("accuracy", "benchmark"))
    service = EvaluationAgentService(
        _SemanticBackend(
            EvaluationCoordinator(
                executor,
                InMemoryEvaluationStore(),
                clock,
                max_await_timeout_s=MAX_AGENT_AWAIT_S,
            )
        ),
        _namespace(tmp_path),
        tmp_path / "evaluation.sock",
    )
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    started = clock.monotonic()
    await service.start()
    try:
        tool = next(
            tool
            for tool in build_evaluation_tools(
                socket_path=service.socket_path,
                token=grant.token,
                role=EvaluationAgentRole.IMPLEMENTER,
            )
            if tool.name == "await_evaluation"
        )
        args = tool.input_schema.model_validate(
            {"handle_id": submitted.handle_id, "timeout_s": requested_s}
        )
        raw = await asyncio.to_thread(tool.handler, args)
    finally:
        await service.close()

    assert f"{MAX_AGENT_AWAIT_S:.0f} s" in tool.description
    reply = AwaitReply.model_validate_json(raw)
    assert isinstance(reply.result, EvaluationStillRunning)
    assert executor.wait_calls[0][1] == MAX_AGENT_AWAIT_S
    assert clock.monotonic() - started == MAX_AGENT_AWAIT_S


# The smallest MCP tool-call timeout a supported agent CLI is known to apply
# (Codex's documented 60 s default). An await call must return before it.
_SMALLEST_CLIENT_TOOL_TIMEOUT_S = 60.0
# The socket read slack the MCP tool adds to the await bound.
_SOCKET_SLACK_S = 5.0


async def _await_through_tool(
    service: EvaluationAgentService, token: str, handle_id: str, timeout_s: float
) -> AwaitReply:
    """Call await_evaluation over the real socket, as an agent CLI does."""
    await service.start()
    try:
        tool = next(
            tool
            for tool in build_evaluation_tools(
                socket_path=service.socket_path,
                token=token,
                role=EvaluationAgentRole.IMPLEMENTER,
            )
            if tool.name == "await_evaluation"
        )
        args = tool.input_schema.model_validate({"handle_id": handle_id, "timeout_s": timeout_s})
        raw = await asyncio.to_thread(tool.handler, args)
    finally:
        await service.close()
    return AwaitReply.model_validate_json(raw)


def _bounded_service(tmp_path: Path) -> tuple[EvaluationAgentService, FakeEvaluationExecutor]:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, supported_evidence_kinds=("accuracy", "benchmark"))
    service = EvaluationAgentService(
        _SemanticBackend(
            EvaluationCoordinator(
                executor,
                InMemoryEvaluationStore(),
                clock,
                max_await_timeout_s=MAX_AGENT_AWAIT_S,
            )
        ),
        _namespace(tmp_path),
        tmp_path / "evaluation.sock",
    )
    return service, executor


async def _produced_accuracy_progress(
    executor: FakeEvaluationExecutor, handle_id: str
) -> EvaluationStepResult:
    original = next(item for item in executor.submissions if item.handle_id == handle_id)
    capture = SemanticEvaluationStage.model_validate(original.request.stages[0].payload)
    with TemporaryDirectory(prefix="recorded-progress-") as directory:
        async with build_scenario(
            Path(directory),
            ScenarioSpec(
                revision=capture.snapshot,
                patch=capture.snapshot,
                scope_id=original.request.owner_scope,
                kinds=tuple(EvidenceKind(stage.name) for stage in original.request.stages),
            ),
        ) as scenario:
            assert scenario.submission.handle_id == handle_id
            return scenario.record.stage_results[0]


@pytest.mark.asyncio
async def test_await_returns_recorded_progress_at_the_bound_before_any_client_timeout(
    tmp_path: Path,
) -> None:
    service, executor = _bounded_service(tmp_path)
    grant = service.grant(
        principal_id="implementer-1", role=EvaluationAgentRole.IMPLEMENTER, scope_id=None
    )
    submitted = await service.dispatch(
        SubmitCall(
            token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK)
        )
    )
    assert isinstance(submitted, SubmittedReply)
    accuracy = await _produced_accuracy_progress(executor, submitted.handle_id)
    executor.set_state(
        submitted.handle_id,
        EvaluationState.RUNNING,
        current_stage="benchmark",
        stage_results=(accuracy,),
    )
    started = executor.clock.monotonic()

    reply = await _await_through_tool(service, grant.token, submitted.handle_id, 1800.0)

    waited = executor.clock.monotonic() - started
    assert waited == MAX_AGENT_AWAIT_S
    assert waited + _SOCKET_SLACK_S < _SMALLEST_CLIENT_TOOL_TIMEOUT_S
    assert reply.result == EvaluationStillRunning(
        handle_id=submitted.handle_id,
        state=EvaluationState.RUNNING,
        current_stage="benchmark",
        next_await_s=MAX_AGENT_AWAIT_S,
    )


@pytest.mark.asyncio
async def test_await_returns_the_result_when_the_evaluation_finishes_within_the_bound(
    tmp_path: Path,
) -> None:
    service, executor = _bounded_service(tmp_path)
    grant = service.grant(
        principal_id="implementer-1", role=EvaluationAgentRole.IMPLEMENTER, scope_id=None
    )
    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    accuracy = await _produced_accuracy_progress(executor, submitted.handle_id)
    executor.script_wait_transition(
        ExecutorObservation(state=EvaluationState.SUCCEEDED, stage_results=(accuracy,)),
        elapsed_s=MAX_AGENT_AWAIT_S - 1,
    )
    started = executor.clock.monotonic()

    reply = await _await_through_tool(service, grant.token, submitted.handle_id, 1800.0)

    assert executor.clock.monotonic() - started == MAX_AGENT_AWAIT_S - 1
    assert reply.result == EvaluationCompleted(handle_id=submitted.handle_id, stages=(accuracy,))


@pytest.mark.asyncio
async def test_service_close_cancels_remembered_execution(tmp_path: Path) -> None:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, supported_evidence_kinds=("accuracy", "benchmark"))
    backend = _BlockingSemanticBackend(
        EvaluationCoordinator(
            executor,
            InMemoryEvaluationStore(),
            clock,
            max_await_timeout_s=20,
        )
    )
    service = EvaluationAgentService(
        backend,
        _namespace(tmp_path),
        tmp_path / "evaluation.sock",
    )
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    await service.start()

    _reader, writer = await asyncio.open_unix_connection(service.socket_path)
    writer.write(
        AwaitCall(
            token=grant.token,
            handle_id=submitted.handle_id,
            timeout_s=10,
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )
    await writer.drain()
    await backend.await_started.wait()
    writer.close()
    await service.close()

    assert await backend.status(submitted.handle_id) is EvaluationState.CANCELED


@pytest.mark.asyncio
async def test_abrupt_client_disconnect_is_normal_socket_teardown(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = _BlockingAvailabilityBackend(
        EvaluationCoordinator(
            FakeEvaluationExecutor(clock),
            InMemoryEvaluationStore(),
            clock,
            max_await_timeout_s=20,
        )
    )
    service = EvaluationAgentService(
        backend,
        _namespace(tmp_path),
        tmp_path / "evaluation.sock",
    )
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id=None,
    )
    loop = asyncio.get_running_loop()
    reported: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    await service.start()
    try:
        _reader, writer = await asyncio.open_unix_connection(service.socket_path)
        writer.write(AvailabilityCall(token=grant.token).model_dump_json().encode() + b"\n")
        await writer.drain()
        await backend.availability_started.wait()
        writer.transport.abort()
        backend.release_availability.set()
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        await service.close()
        loop.set_exception_handler(previous_handler)

    assert reported == []


@pytest.mark.asyncio
async def test_a_stopping_run_refuses_new_submissions_and_profiles_with_a_typed_reply(
    tmp_path: Path,
) -> None:
    stopping = [False]
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, supported_evidence_kinds=("accuracy", "benchmark"))
    coordinator = EvaluationCoordinator(executor, InMemoryEvaluationStore(), clock)
    namespace = _namespace(tmp_path)
    provision = FakeProfilerTurnProvision()

    async def candidate_snapshot(scope: str | None) -> str:
        return f"snapshot:{scope}"

    async def no_evidence(
        _principal: str, _scope: str | None, _snapshot: str, _ids: tuple[str, ...]
    ) -> tuple[TrustedEvidence, ...]:
        return ()

    profiler = ProfilerAgentService(
        provision, namespace, ProfilerAgentServiceHooks(candidate_snapshot, no_evidence)
    )
    service = EvaluationAgentService(
        _SemanticBackend(coordinator),
        namespace,
        tmp_path / "evaluation.sock",
        profiler,
        stopping=lambda: stopping[0],
    )
    grant = service.grant(
        principal_id="implementer-1", role=EvaluationAgentRole.IMPLEMENTER, scope_id="h1"
    )
    submit = SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    profile = DispatchProfilerCall(
        token=grant.token,
        work=ProfilerWorkKey(purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC, focus="decode"),
        request="Where does decode time go?",
    )

    stopping[0] = True
    assert await service.dispatch(submit) == RunStoppingReply()
    assert await service.dispatch(profile) == RunStoppingReply()
    assert await service.scope_handles("h1") == ()
    assert provision.turns == []

    # A resume reopens submissions: the predicate is read at each request.
    stopping[0] = False
    submitted = await service.dispatch(submit)
    assert isinstance(submitted, SubmittedReply)
    assert await service.scope_handles("h1") == (submitted.handle_id,)
    await profiler.close()


@pytest.mark.parametrize("role", list(EvaluationAgentRole))
def test_suspension_tool_surface_preserves_other_tools(
    role: EvaluationAgentRole, tmp_path: Path
) -> None:
    """Suspension omits bounded evaluation waits while preserving existing callers."""
    token = role.value
    ordinary = build_evaluation_tools(socket_path=tmp_path / "unused", token=token, role=role)
    suspended = build_evaluation_tools(
        socket_path=tmp_path / "unused",
        token=token,
        role=role,
        evaluation_suspension=True,
    )
    assert {tool.name for tool in suspended} == {
        *(tool.name for tool in ordinary if tool.name != "await_evaluation"),
        "validate_evaluation_wait",
    }


@pytest.mark.parametrize("suspension", [False, True])
def test_suspension_grant_matches_descriptor_and_retains_identity(
    tmp_path: Path, *, suspension: bool
) -> None:
    """The selected capability reaches stdio and cannot reuse a different grant."""
    service, _executor = _service(tmp_path)
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate-1",
        evaluation_suspension=suspension,
    )
    repeat = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate-1",
        evaluation_suspension=suspension,
    )
    other = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate-1",
        evaluation_suspension=not suspension,
    )
    assert repeat is grant
    assert other.token != grant.token
    environment = dict(evaluation_mcp_descriptor(grant, str(service.socket_path)).env)
    assert environment["VS_EVALUATION_SUSPENSION"] == ("1" if suspension else "0")


def _bind_restart_session(
    service: EvaluationAgentService,
    client: AgentClient,
    workspace: Path,
    key: AgentSessionKey,
) -> tuple[EvaluationGrant, AgentSessionSpec, ClientAgentSessions]:
    grant = service.grant(
        principal_id="implementer-1",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate-1",
        evaluation_suspension=True,
    )
    descriptor = evaluation_mcp_descriptor(grant, str(service.socket_path))
    # The low-level session API receives the same public descriptor that
    # the convenience invocation API translates for runtime callers.
    spec = AgentSessionSpec(
        role="implementer",
        provider="claude",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
        mcp_servers=(
            MCPServerSpec(
                **{
                    field.name: getattr(descriptor, field.name)
                    for field in fields(MCPServerSpec)
                    if field.init
                }
            ),
        ),
    )
    sessions = ClientAgentSessions(
        client,
        _namespace(workspace).slot("invocations.json", AgentInvocationState),
    )
    sessions.bind(key, spec, AgentTurnRequest(message=""))
    return grant, spec, sessions


def _restart_host(
    workspace: Path,
    scratch: Path,
    remote: FakeEvaluationBackend,
    credentials: list[str],
) -> tuple[EvaluationAgentService, FakeEvaluationExecutor, AgentClient]:
    namespace = _namespace(workspace)
    clock = FakeClock()
    executor = FakeEvaluationExecutor(
        clock,
        backend=remote,
        supported_evidence_kinds=("accuracy",),
    )
    coordinator = EvaluationCoordinator(
        executor,
        FilesystemEvaluationStore(scratch / "evaluations"),
        clock,
        max_await_timeout_s=20,
    )
    service = EvaluationAgentService(
        _SemanticBackend(coordinator),
        namespace,
        scratch / "evaluation.sock",
    )

    def execute(request: agentshim.CommandRequest) -> FakeRun:
        config = installed_mcp_servers("claude", request, workspace)
        credentials.append(config["vs-evaluation"]["env"]["VS_EVALUATION_TOKEN"])
        return scripted_turn("claude", session_id="session-1", text="waiting")

    driver = fake_agentshim_driver(provider="claude", executor=FakeExecutor(execute))
    client = AgentClient(
        driver,
        provider="claude",
        session_store=DurableSessionStore(
            namespace.slot("sessions.json", AgentSessionState),
        ),
    )
    return service, executor, client


@pytest.mark.asyncio
async def test_waiting_session_resumes_with_fresh_credentials_after_host_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebuild all host services from disk while the remote evaluation survives."""
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state"))
    remote = FakeEvaluationBackend()
    workspace = tmp_path / "workspace"
    namespace = _namespace(workspace)
    key = AgentSessionKey(SessionScope.MEMBER, "implementer-1")

    credentials: list[str] = []

    service, executor, client = _restart_host(workspace, tmp_path, remote, credentials)
    grant, spec, sessions = _bind_restart_session(service, client, workspace, key)
    first = client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    assert isinstance(submitted, SubmittedReply)
    assert await executor.inspect(submitted.handle_id) is not None
    checkpoint = sessions.checkpoint(key)
    client.close()

    restarted, restarted_executor, resumed_client = _restart_host(
        workspace, tmp_path, remote, credentials
    )
    fresh, _fresh_spec, resumed_sessions = _bind_restart_session(
        restarted, resumed_client, workspace, key
    )
    assert fresh.token != grant.token
    # Durable authorization records are loaded, but the old process's bearer
    # credential is rejected and the resumed session gets the new credential.
    with pytest.raises(EvaluationAgentAccessError, match="invalid"):
        await restarted.dispatch(StatusCall(token=grant.token, handle_id=submitted.handle_id))
    status = await restarted.dispatch(StatusCall(token=fresh.token, handle_id=submitted.handle_id))
    assert isinstance(status, StatusReply)
    assert status.status is EvaluationState.QUEUED
    accuracy = await _produced_accuracy_progress(executor, submitted.handle_id)
    restarted_executor.set_observation(
        submitted.handle_id,
        ExecutorObservation(
            state=EvaluationState.SUCCEEDED,
            stage_results=(accuracy,),
        ),
    )
    settled = await restarted.dispatch(StatusCall(token=fresh.token, handle_id=submitted.handle_id))
    assert isinstance(settled, StatusReply)
    assert settled.status is EvaluationState.SUCCEEDED
    (workspace / "resume.j2").write_text("Evaluation settled", encoding="utf-8")
    message = TemplateRenderer(workspace).render_template("resume.j2")
    try:
        result = resumed_sessions.resume(key, message, "resume-1")
        assert isinstance(result, Completed)
        assert result.checkpoint == checkpoint
        assert result.result.provider_session_id == first.provider_session_id
        assert credentials == [grant.token, fresh.token]
        authenticated = await restarted.dispatch(
            StatusCall(
                token=credentials[-1],
                handle_id=submitted.handle_id,
            )
        )
        assert isinstance(authenticated, StatusReply)
        assert authenticated.status is EvaluationState.SUCCEEDED
        assert resumed_sessions.inspect(key, "resume-1") == result
        persisted = namespace.slot("sessions.json", AgentSessionState).load_optional()
        assert persisted is not None
        persisted_json = persisted.model_dump_json()
        assert grant.token not in persisted_json
        assert fresh.token not in persisted_json
    finally:
        resumed_client.close()


@given(
    principal=st.text(min_size=1, max_size=30),
    scope=st.one_of(st.none(), st.text(max_size=30)),
    role=st.sampled_from(list(EvaluationAgentRole)),
    capabilities=st.tuples(st.booleans(), st.booleans(), st.booleans()),
)
def test_evaluation_session_identity_tracks_authority_but_allows_credential_rotation(
    principal: str,
    scope: str | None,
    role: EvaluationAgentRole,
    capabilities: tuple[bool, bool, bool],
) -> None:
    """Every grant authority dimension remains a checkpoint compatibility fence."""
    grant = EvaluationGrant(
        token="old-secret-" + "x" * 32,
        principal_id=principal,
        scope_id=scope,
        role=role,
        profiler_available=capabilities[0],
        run_observer=capabilities[1],
        evaluation_suspension=capabilities[2],
    )
    descriptor = evaluation_mcp_descriptor(grant, "/old/service.sock")
    changed_token = EvaluationGrant.model_validate(
        {
            **grant.model_dump(),
            "token": "new-secret-" + "y" * 32,
        }
    )
    refreshed = evaluation_mcp_descriptor(changed_token, "/new/service.sock")
    assert descriptor == refreshed
    assert repr(descriptor) == repr(refreshed)
    assert grant.token not in repr(descriptor)
    assert changed_token.token not in repr(refreshed)
    changes = {
        "principal_id": principal + "-other",
        "scope_id": "" if scope is None else None,
        "role": next(other for other in EvaluationAgentRole if other is not role),
        "profiler_available": not grant.profiler_available,
        "run_observer": not grant.run_observer,
        "evaluation_suspension": not grant.evaluation_suspension,
    }
    for key, value in changes.items():
        changed_grant = EvaluationGrant.model_validate({**grant.model_dump(), key: value})
        assert evaluation_mcp_descriptor(changed_grant, "/old/service.sock") != descriptor


@pytest.mark.asyncio
async def test_same_scope_cached_submission_preserves_first_admission_order(tmp_path: Path) -> None:
    service, _executor = _service(tmp_path)
    grant = service.grant(
        principal_id="implementer",
        role=EvaluationAgentRole.IMPLEMENTER,
        scope_id="candidate",
    )
    first = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    second = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.BENCHMARK,))
    )
    cached = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    )
    third = await service.dispatch(
        SubmitCall(
            token=grant.token,
            evidence_kinds=(
                EvidenceKind.ACCURACY,
                EvidenceKind.BENCHMARK,
            ),
        )
    )
    assert isinstance(first, SubmittedReply)
    assert isinstance(second, SubmittedReply)
    assert isinstance(cached, SubmittedReply)
    assert isinstance(third, SubmittedReply)
    assert cached.handle_id == first.handle_id
    assert await service.scope_handles("candidate") == (
        first.handle_id,
        second.handle_id,
        third.handle_id,
    )


def test_dynamic_profiler_caller_is_offered_yield_and_nonblocking_status(tmp_path: Path) -> None:
    """The dynamic caller cannot spend worker time on profiler polling tools."""
    tools = build_evaluation_tools(
        socket_path=tmp_path / "service.sock",
        token="x" * 32,
        role=EvaluationAgentRole.IMPLEMENTER,
        profiler_available=True,
        evaluation_suspension=True,
    )
    names = {tool.name for tool in tools}
    assert {"dispatch_profiler", "profiler_status"} <= names
    assert not {"await_profiler", "await_evaluation"} & names
