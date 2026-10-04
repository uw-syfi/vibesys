"""Requester association contracts over the real semantic submission producer."""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, JsonValue, RootModel
from tests.support.evaluation_scenarios import ScenarioSpec, build_scenario

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.run.evaluation_backend import (
    EvidenceReusingEvaluation,
    SemanticEvaluationBackend,
    SemanticEvaluationIdentity,
)
from vs_evaluation.api import (
    EVALUATION_ACCESS_STATE_PATH,
    CancelCall,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationAgentState,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationDependencyError,
    EvaluationFailed,
    EvaluationPending,
    EvaluationState,
    EvidenceKind,
    OwnedEvaluationDependencies,
    SettlementErrorCode,
    SubmitCall,
    SubmittedReply,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationExecutor,
    FakeEvaluationSettlements,
    InMemoryEvaluationNamespace,
)
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
)
from vs_runtime.api import AgentRole, AgentToolBindingContext, RuntimeContractError
from vs_runtime.api.infrastructure import create_run_control_channel, stop_gated_evaluation
from vs_runtime.api.testing import (
    FakeEvaluation,
    FakeRun,
    FakeRunControlEventSink,
    FakeWorkspace,
    FakeWorkspaces,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_evaluation.api import (
        EvaluationSettlements,
        EvaluationStateNamespace,
        EvaluationStepResult,
        ExecutorObservation,
    )


@dataclass
class _Harness:
    service: EvaluationAgentService
    settlements: EvaluationSettlements
    backend: SemanticEvaluationBackend
    executor: FakeEvaluationExecutor
    namespace: InMemoryEvaluationNamespace
    tokens: dict[str, str]
    successful_stages: tuple[EvaluationStepResult, ...]
    successful_handle: str
    kind: EvidenceKind = EvidenceKind.ACCURACY

    async def submit(self, scope: str) -> str:
        reply = await self.service.dispatch(
            SubmitCall(token=self.tokens[scope], evidence_kinds=(self.kind,))
        )
        assert isinstance(reply, SubmittedReply)
        return reply.handle_id

    async def cancel(self, scope: str, handle: str) -> None:
        await self.service.dispatch(CancelCall(token=self.tokens[scope], handle_id=handle))

    def dependency(
        self, scope: str, handle: str, generation: int = 0
    ) -> OwnedEvaluationDependencies:
        return OwnedEvaluationDependencies(scope_id=scope, generation=generation, handles=(handle,))

    async def complete(self, handle: str) -> None:
        self.executor.set_state(
            handle, EvaluationState.SUCCEEDED, stage_results=self.successful_stages
        )
        await self.backend.status(handle)


@asynccontextmanager
async def _harness(
    root: Path,
    implementation: str,
    kind: EvidenceKind = EvidenceKind.ACCURACY,
    *,
    namespace: InMemoryEvaluationNamespace | None = None,
    executor: FakeEvaluationExecutor | None = None,
) -> AsyncIterator[_Harness]:
    spec = ScenarioSpec(kinds=(kind,), patch="shared candidate")
    async with build_scenario(root / "producer", spec) as produced:
        successful_stages = produced.record.stage_results
        successful_handle = produced.submission.handle_id
        fingerprints = produced.submission.fingerprints
    namespace = InMemoryEvaluationNamespace() if namespace is None else namespace
    executor = (
        FakeEvaluationExecutor(FakeClock(), supported_evidence_kinds=(kind.value,))
        if executor is None
        else executor
    )
    run = FakeRun(PLUGIN, project_root=root / "project", supports_parallel_candidates=True)
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=root / "project"), supports_parallel_candidates=True
    )
    workspaces.set_default_patch(spec.patch)
    backend = SemanticEvaluationBackend(
        run.evaluation,
        workspaces,
        namespace,
        SemanticEvaluationIdentity(
            evaluator=fingerprints.evaluator,
            workload=fingerprints.workload,
            environment=fingerprints.environment,
        ),
        executor=executor,
        submitted_time=lambda: 100.0,
    )
    service = EvaluationAgentService(backend, namespace, root / "evaluation.sock")
    tokens = {}
    for member in ("a", "b", "c"):
        workspace = await workspaces.create_candidate(member_id=member)
        backend.bind(
            AgentToolBindingContext(
                role=AgentRole(id="implementer", system_prompt="test"),
                workspace=workspace,
                member_id=member,
                agent_path=str,
            )
        )
        assert workspace.id is not None
        tokens[workspace.id] = service.grant(
            principal_id=f"implementer:{workspace.id}",
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=workspace.id,
        ).token
    settlements = (
        FakeEvaluationSettlements(backend=backend, namespace=namespace)
        if implementation == "fake"
        else service.settlements()
    )
    try:
        yield _Harness(
            service,
            settlements,
            backend,
            executor,
            namespace,
            tokens,
            successful_stages,
            successful_handle,
            kind,
        )
    finally:
        await service.cancel_outstanding()
        await run.close()
        await workspaces.close()


@pytest.fixture(params=("fake", "service"))
def implementation(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class _InspectionGateExecutor(FakeEvaluationExecutor):
    """Delay an executor observation while keeping its original observation revision."""

    _inspection_gate: asyncio.Event | None = None

    def pause_next_inspection(self) -> tuple[asyncio.Event, asyncio.Event]:
        self.inspection_started = asyncio.Event()
        self._inspection_gate = asyncio.Event()
        return self.inspection_started, self._inspection_gate

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        observation = await super().inspect_only(handle_id)
        gate = self._inspection_gate
        self._inspection_gate = None
        if gate is not None:
            self.inspection_started.set()
            await gate.wait()
        return observation


class _CancellationIntentNamespace(InMemoryEvaluationNamespace):
    """Observe the real service's durable final-requester withdrawal transition."""

    def __init__(self) -> None:
        super().__init__()
        self.cancellation_committed = asyncio.Event()

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        super().save(relative_path, model)
        if (
            relative_path == EVALUATION_ACCESS_STATE_PATH
            and isinstance(model, EvaluationAgentState)
            and any(access.cancel_pending for access in model.handles)
        ):
            self.cancellation_committed.set()


@pytest.mark.asyncio
async def test_join_reselects_when_canonical_cancellation_wins_admission(
    tmp_path: Path, implementation: str
) -> None:
    namespace = _CancellationIntentNamespace()
    executor = _InspectionGateExecutor(FakeClock(), supported_evidence_kinds=("accuracy",))
    async with _harness(
        tmp_path, implementation, namespace=namespace, executor=executor
    ) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        original = await harness.submit(owner)
        inspection_started, release = executor.pause_next_inspection()
        joining = asyncio.create_task(harness.submit(joined))
        await inspection_started.wait()
        cancelling = asyncio.create_task(harness.cancel(owner, original))
        await namespace.cancellation_committed.wait()
        release.set()
        fresh, _ = await asyncio.gather(joining, cancelling)
        assert fresh != original
        assert len(executor.submissions) == 2
        assert executor.cancellations == [original]
        assert await harness.backend.status(fresh) is EvaluationState.QUEUED
        accesses = namespace.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState).handles
        original_access = next(access for access in accesses if access.handle_id == original)
        assert all(item.scope_id != joined for item in original_access.associations)
        await harness.complete(fresh)
        result = await harness.settlements.wait_any(harness.dependency(joined, fresh))
        assert isinstance(result[0].result, EvaluationCompleted)


@pytest.mark.asyncio
async def test_joined_cancel_drops_only_requester_association(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        handle = await harness.submit(owner)
        assert await harness.submit(joined) == handle
        assert len(harness.executor.submissions) == 1
        await harness.cancel(joined, handle)
        assert harness.executor.cancellations == []
        assert isinstance(
            (await harness.settlements.observe(harness.dependency(owner, handle)))[0].result,
            EvaluationPending,
        )
        with pytest.raises(EvaluationDependencyError) as error:
            await harness.settlements.observe(harness.dependency(joined, handle))
        assert error.value.code is SettlementErrorCode.UNOWNED
        await harness.complete(handle)
        result = await harness.settlements.wait_any(harness.dependency(owner, handle))
        assert isinstance(result[0].result, EvaluationCompleted)
        assert (await harness.backend.recorded_snapshot(handle)).request.owner_scope == owner


@pytest.mark.asyncio
async def test_canonical_cancel_keeps_capture_while_another_requester_waits(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        handle = await harness.submit(owner)
        assert await harness.submit(joined) == handle
        await harness.cancel(owner, handle)
        assert harness.executor.cancellations == []
        await harness.complete(handle)
        result = await harness.settlements.wait_any(harness.dependency(joined, handle))
        assert isinstance(result[0].result, EvaluationCompleted)
        assert (await harness.backend.recorded_snapshot(handle)).request.owner_scope == owner


@pytest.mark.asyncio
async def test_joined_orchestrator_cancel_only_releases_its_association(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation, EvidenceKind.BENCHMARK) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        harness.tokens[joined] = harness.service.grant(
            principal_id=f"orchestrator:{joined}",
            role=EvaluationAgentRole.ORCHESTRATOR,
            scope_id=joined,
        ).token
        handle = await harness.submit(owner)
        assert await harness.submit(joined) == handle
        await harness.cancel(joined, handle)
        assert harness.executor.cancellations == []
        await harness.complete(handle)
        result = await harness.settlements.wait_any(harness.dependency(owner, handle))
        assert isinstance(result[0].result, EvaluationCompleted)


@pytest.mark.asyncio
async def test_last_requester_cancel_releases_capture_once(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        handle = await harness.submit(owner)
        assert await harness.submit(joined) == handle
        await harness.cancel(joined, handle)
        await harness.cancel(owner, handle)
        await harness.cancel(owner, handle)
        assert harness.executor.cancellations == [handle]
        assert await harness.backend.status(handle) is EvaluationState.CANCELED


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_joined_dependency_validates_requester_scope(
    tmp_path: Path, implementation: str, *, completed: bool
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        handle = await harness.submit(owner)
        if completed:
            await harness.complete(handle)
        assert await harness.submit(joined) == handle
        observation = (await harness.settlements.observe(harness.dependency(joined, handle)))[0]
        assert observation.scope_id == joined
        assert observation.generation == 0
        assert isinstance(
            observation.result, EvaluationCompleted if completed else EvaluationPending
        )
        assert (await harness.backend.recorded_snapshot(handle)).request.owner_scope == owner


@pytest.mark.asyncio
async def test_terminal_rejoin_records_current_requester_generation(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner = next(iter(harness.tokens))
        handle = await harness.submit(owner)
        await harness.complete(handle)
        await harness.service.cancel_scope(owner)
        await harness.service.reopen_scope(owner)
        assert await harness.submit(owner) == handle
        result = await harness.settlements.wait_any(harness.dependency(owner, handle, generation=1))
        assert isinstance(result[0].result, EvaluationCompleted)
        assert result[0].generation == 1
        assert (await harness.backend.recorded_snapshot(handle)).request.owner_generation == 0
        with pytest.raises(EvaluationDependencyError) as error:
            await harness.settlements.observe(harness.dependency(owner, handle))
        assert error.value.code is SettlementErrorCode.STALE_GENERATION


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_reopened_joined_scope_uses_its_current_generation(
    tmp_path: Path, implementation: str, *, completed: bool
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        handle = await harness.submit(owner)
        assert await harness.submit(joined) == handle
        if completed:
            await harness.complete(handle)
        await harness.service.cancel_scope(joined)
        await harness.service.reopen_scope(joined)
        assert await harness.submit(joined) == handle
        dependency = harness.dependency(joined, handle, generation=1)
        observation = (await harness.settlements.observe(dependency))[0]
        assert observation.scope_id == joined
        assert observation.generation == 1
        assert isinstance(
            observation.result, EvaluationCompleted if completed else EvaluationPending
        )
        record = await harness.backend.recorded_snapshot(handle)
        assert (record.request.owner_scope, record.request.owner_generation) == (owner, 0)
        assert len(harness.executor.submissions) == 1


@pytest.mark.asyncio
async def test_joined_history_survives_requester_cancel_and_service_restart(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner, joined = tuple(harness.tokens)[:2]
        handle = await harness.submit(owner)
        assert await harness.submit(joined) == handle
        assert await harness.service.scope_handles(joined) == (handle,)
        await harness.cancel(joined, handle)
        restarted = EvaluationAgentService(
            harness.backend, harness.namespace, tmp_path / "next.sock"
        )
        assert await restarted.scope_handles(joined) == (handle,)
        assert await harness.backend.owned_handles(joined) == ()


@pytest.mark.asyncio
async def test_joining_host_capture_preserves_host_cancellation_authority(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        capture = await harness.backend.submit_revision_evidence(
            "fake-revision", (EvidenceKind.ACCURACY,)
        )
        joined = next(iter(harness.tokens))
        assert await harness.submit(joined) == capture.handle_id
        await harness.cancel(joined, capture.handle_id)
        assert harness.executor.cancellations == []
        assert await harness.backend.status(capture.handle_id) is EvaluationState.QUEUED
        assert (
            await harness.backend.recorded_snapshot(capture.handle_id)
        ).request.owner_scope is None
        state = harness.namespace.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
        assert state.handles[0].owners == frozenset()


@pytest.mark.asyncio
async def test_final_cancel_intent_replays_after_lost_executor_response(
    tmp_path: Path, implementation: str
) -> None:
    async with _harness(tmp_path, implementation) as harness:
        owner = next(iter(harness.tokens))
        handle = await harness.submit(owner)
        harness.executor.fail_cancel_once = True
        with pytest.raises(OSError, match=r"^$"):
            await harness.cancel(owner, handle)
        state = harness.namespace.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
        assert state.handles[0].cancel_pending
        assert all(not association.active for association in state.handles[0].associations)
        _restart(harness, tmp_path, implementation)
        await harness.service.reconcile_associations()
        assert await harness.backend.status(handle) is EvaluationState.CANCELED
        state = harness.namespace.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
        assert not state.handles[0].cancel_pending
        assert await harness.submit(owner) != handle
        assert len(harness.executor.submissions) == 2


@pytest.fixture(params=["memory", "project"])
def persistence(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> EvaluationStateNamespace:
    if request.param == "memory":
        return InMemoryEvaluationNamespace()
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path.parent / "state-home"))
    project = Project.open(tmp_path)
    project.state.create_project("contract")
    manifest = project.state.new_run_manifest(
        "Legacy evaluation access",
        run_id="legacy-access-contract",
        trusted_input_baseline="a" * 40,
        branch="test/legacy-access",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=RunExecutionRecord(
            model="test",
            agent_backend="fake",
            compute_backend="cpu",
            requested_profiler="none",
            resolved_profiler="none",
            agent_roles={},
        ),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    return project.state.portable_namespace(manifest.run_id, "legacy-access")


def test_pre_association_access_record_loads_and_round_trips(
    persistence: EvaluationStateNamespace,
) -> None:
    digest = {"algorithm": "sha256", "value": "a" * 64}
    previous = {
        "schema_version": 1,
        "handles": [
            {
                "handle_id": "legacy-capture",
                "scope_id": "m-a",
                "fingerprints": dict.fromkeys(
                    ("candidate", "evaluator", "workload", "environment"), digest
                ),
                "kinds": ["accuracy"],
                "observers": ["implementer:m-a"],
                "owners": ["implementer:m-a"],
            }
        ],
    }
    # Write exactly the former schema before loading with the current contract.
    legacy = RootModel[dict[str, JsonValue]].model_validate_json(json.dumps(previous))
    persistence.save(EVALUATION_ACCESS_STATE_PATH, legacy)
    parsed = EvaluationAgentState.model_validate_json(json.dumps(previous))
    loaded = persistence.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
    assert loaded == parsed
    assert loaded.handles[0].scope_id == "m-a"
    assert loaded.handles[0].owners == frozenset({"implementer:m-a"})
    assert EvaluationAgentState.model_validate_json(loaded.model_dump_json()) == parsed
    persistence.save(EVALUATION_ACCESS_STATE_PATH, loaded)
    assert persistence.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState) == parsed


_Operation = st.tuples(
    st.sampled_from(("submit", "join", "cancel", "settle", "complete", "reconcile", "restart")),
    st.integers(min_value=0, max_value=2),
)


async def _interleave(
    root: Path, implementation: str, requester_count: int, operations: list[tuple[str, int]]
) -> None:
    async with _harness(root, implementation) as harness:
        scopes = tuple(harness.tokens)[:requester_count]
        waiting: set[tuple[str, str]] = set()
        failed: set[str] = set()
        settled: set[tuple[str, str]] = set()
        for action, index in operations:
            scope = scopes[index % requester_count]
            if action in {"submit", "join"}:
                handle = await harness.submit(scope)
                assert handle not in failed
                if (scope, handle) not in settled:
                    waiting.add((scope, handle))
            elif action == "cancel":
                await _withdraw(harness, waiting, scope)
            elif action in {"settle", "complete"}:
                failed |= _finish_requested(harness, waiting, scope, action, index)
            elif action == "reconcile":
                await _settle_ready(harness, waiting, settled)
            elif action == "restart":
                _restart(harness, root, implementation)
            assert harness.executor.backend.active_count <= 1
        for handle in {handle for _, handle in waiting}:
            remote = harness.executor.backend.inspect(handle)
            if remote is not None and remote.state in {
                EvaluationState.QUEUED,
                EvaluationState.STARTING,
                EvaluationState.RUNNING,
            }:
                harness.executor.set_state(handle, EvaluationState.FAILED, failure="final failure")
        await _settle_ready(harness, waiting, settled)
    assert not waiting


def _finish_requested(
    harness: _Harness,
    waiting: set[tuple[str, str]],
    scope: str,
    action: str,
    index: int,
) -> set[str]:
    failed = set()
    states = (EvaluationState.FAILED, EvaluationState.CANCELED, EvaluationState.SUPERSEDED)
    for requester, handle in sorted(waiting):
        remote = harness.executor.backend.inspect(handle)
        if (
            requester != scope
            or remote is None
            or remote.state
            in {
                EvaluationState.SUCCEEDED,
                *states,
            }
        ):
            continue
        if action == "complete":
            if handle == harness.successful_handle:
                harness.executor.set_state(
                    handle, EvaluationState.SUCCEEDED, stage_results=harness.successful_stages
                )
        else:
            state = states[index % len(states)]
            harness.executor.set_state(
                handle, state, failure="remote failure" if state is EvaluationState.FAILED else None
            )
            if state is EvaluationState.FAILED:
                failed.add(handle)
    return failed


async def _settle_ready(
    harness: _Harness, waiting: set[tuple[str, str]], settled: set[tuple[str, str]]
) -> None:
    for scope, handle in sorted(waiting):
        result = (await harness.settlements.inspect(harness.dependency(scope, handle)))[0].result
        if isinstance(result, EvaluationCompleted | EvaluationFailed | EvaluationCanceled):
            observed = await harness.settlements.wait_any(harness.dependency(scope, handle))
            assert observed[0].result == result
            assert (scope, handle) not in settled
            settled.add((scope, handle))
            waiting.remove((scope, handle))


async def _withdraw(harness: _Harness, waiting: set[tuple[str, str]], scope: str) -> None:
    for requester, handle in sorted(waiting):
        if requester != scope:
            continue
        waiting.remove((scope, handle))
        others = any(item == handle for _, item in waiting)
        before = await harness.backend.status(handle)
        await harness.cancel(scope, handle)
        if others:
            assert await harness.backend.status(handle) is before


def _restart(harness: _Harness, root: Path, implementation: str) -> None:
    harness.service = EvaluationAgentService(
        harness.backend, harness.namespace, root / "restart.sock"
    )
    harness.tokens = {
        scope: harness.service.grant(
            principal_id=f"implementer:{scope}",
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=scope,
        ).token
        for scope in harness.tokens
    }
    harness.settlements = (
        FakeEvaluationSettlements(backend=harness.backend, namespace=harness.namespace)
        if implementation == "fake"
        else harness.service.settlements()
    )


@pytest.mark.parametrize("implementation", ["fake", "service"])
@settings(max_examples=30, deadline=None)
@example(requester_count=2, operations=[("submit", 0), ("join", 1), ("cancel", 1)])
@example(requester_count=3, operations=[("submit", 0), ("settle", 0), ("join", 1)])
@given(
    requester_count=st.integers(min_value=2, max_value=3),
    operations=st.lists(_Operation, max_size=20),
)
def test_requester_interleavings_preserve_capture_and_settlement_contract(
    implementation: str, requester_count: int, operations: list[tuple[str, int]]
) -> None:
    with tempfile.TemporaryDirectory(prefix="shared-evaluation-") as directory:
        asyncio.run(_interleave(Path(directory), implementation, requester_count, operations))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name", ["submitted_generation", "cancel_submitted", "submitted_report"]
)
@pytest.mark.parametrize("bounded", [False, True])
async def test_runtime_requester_methods_reject_missing_scope(
    tmp_path: Path,
    implementation: str,
    method_name: str,
    *,
    bounded: bool,
) -> None:
    """The Fake and production contract both require explicit requester authority."""
    async with _harness(tmp_path, implementation) as harness:
        evaluation = (
            FakeEvaluation(settlement_observations=harness.settlements)
            if implementation == "fake"
            else EvidenceReusingEvaluation(
                FakeEvaluation(), harness.backend, run_id="contract", scopes=harness.service
            )
        )
        if bounded:
            evaluation = stop_gated_evaluation(
                evaluation, create_run_control_channel(FakeRunControlEventSink())
            )
        method = getattr(evaluation, method_name)
        with pytest.raises(TypeError, match="scope_id"):
            await method("unknown")


@pytest.mark.asyncio
async def test_runtime_scoped_authority_and_historical_reads(
    tmp_path: Path, implementation: str
) -> None:
    """Scoped reads reject strangers and survive idempotent requester withdrawal."""
    async with _harness(tmp_path, implementation) as harness:
        owner_scope, requester_scope, _ = harness.tokens
        handle = await harness.submit(owner_scope)
        assert await harness.submit(requester_scope) == handle
        report = await harness.backend.recorded_snapshot(handle)
        evaluation = (
            FakeEvaluation(
                settlement_observations=harness.settlements,
                association_cancellation=harness.service.cancel_association,
                submitted_generations={
                    (scope, handle): 0 for scope in (owner_scope, requester_scope)
                },
                submitted_reports={handle: report.model_dump_json()},
            )
            if implementation == "fake"
            else EvidenceReusingEvaluation(
                FakeEvaluation(), harness.backend, run_id="contract", scopes=harness.service
            )
        )
        for method_name in ("submitted_generation", "submitted_report", "cancel_submitted"):
            with pytest.raises((EvaluationDependencyError, RuntimeContractError)):
                await getattr(evaluation, method_name)(handle, scope_id="never-associated")
        await evaluation.cancel_submitted(handle, scope_id=requester_scope)
        await evaluation.cancel_submitted(handle, scope_id=requester_scope)
        assert await evaluation.submitted_generation(handle, scope_id=requester_scope) == 0
        assert (
            await evaluation.submitted_report(handle, scope_id=requester_scope)
            == report.model_dump_json()
        )
        assert harness.executor.cancellations == []
        (pending,) = await harness.settlements.observe(harness.dependency(owner_scope, handle))
        assert isinstance(pending.result, EvaluationPending)
        with pytest.raises(EvaluationDependencyError):
            await harness.settlements.observe(harness.dependency(requester_scope, handle))
