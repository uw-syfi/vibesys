"""Public contract tests for releasing one workspace scope's jobs."""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support.evaluation_scenarios import ScenarioSpec, capture_submission

from vibesys.run.evaluation_backend import SemanticEvaluationStage
from vs_evaluation.api import (
    AvailabilitySnapshot,
    DispatchProfilerCall,
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationAwaitResult,
    EvaluationCoordinator,
    EvaluationOperationSnapshot,
    EvaluationState,
    EvidenceKind,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerDispatchedReply,
    ProfilerOperationState,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    ResourceRequirements,
    RunStoppingReply,
    ScopeRelease,
    ScopeReleasedReply,
    ScopeSubmissionTracker,
    StoredEvaluation,
    SubmitCall,
    SubmittedReply,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
)
from vs_evaluation.api.testing import (
    FakeClock,
    FakeEvaluationExecutor,
    FakeProfilerTurnProvision,
    InMemoryEvaluationStore,
)
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
    StateNamespace,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


_SCOPES = ("m-a", "m-b", "m-c")


class _ContentBackend:
    """Semantic facade Fake whose candidate content the test sets per scope.

    The real producer captures immutable content and original scope ownership.
    Equal content joins the canonical request across requester scopes.
    """

    def __init__(self, coordinator: EvaluationCoordinator) -> None:
        self._coordinator = coordinator
        self._submissions = ScopeSubmissionTracker()
        self.content: dict[str | None, str] = {}

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        return await self._coordinator.availability(requirements)

    async def submit_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
        *,
        own: Callable[[SubmittedSemanticEvaluation], Awaitable[None]],
    ) -> SubmittedSemanticEvaluation:
        async with self._submissions.track(scope_id):
            content = self.content.get(scope_id, scope_id or "root")
            request, submitted = await capture_submission(
                ScenarioSpec(revision=content, patch=content, scope_id=scope_id, kinds=kinds)
            )
            existing = next(
                (
                    record
                    for record in await self._coordinator.history()
                    if record.request.key == request.key
                ),
                None,
            )
            if existing is not None:
                request = existing.request
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
        self, scope_id: str | None, kinds: tuple[EvidenceKind, ...]
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
            evidence_recorded=False,
        )

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        return await self._coordinator.await_result(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> StoredEvaluation:
        return await self._coordinator.cancel(handle_id)


def _namespace(root: Path) -> StateNamespace:
    project = Project.open(root)
    project.state.create_project("test")
    manifest = project.state.new_run_manifest(
        "Scope release test",
        # Machine-local state lives outside ``root``, keyed by its path, so a
        # fresh run ID keeps a reused temporary path from reading old state.
        run_id=f"scope-release-{uuid.uuid4().hex}",
        trusted_input_baseline="a" * 40,
        branch="test/scope-release",
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


@dataclass
class _Harness:
    service: EvaluationAgentService
    profiler: ProfilerAgentService
    provision: FakeProfilerTurnProvision
    executor: FakeEvaluationExecutor
    backend: _ContentBackend
    coordinator: EvaluationCoordinator
    namespace: StateNamespace
    stopping: list[bool] = field(default_factory=lambda: [False])

    def token(self, scope_id: str) -> str:
        return self.service.grant(
            principal_id=f"implementer:{scope_id}",
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=scope_id,
        ).token

    async def submit(self, scope_id: str) -> object:
        return await self.service.dispatch(
            SubmitCall(token=self.token(scope_id), evidence_kinds=(EvidenceKind.ACCURACY,))
        )

    async def profile(self, scope_id: str, session_id: str | None = None) -> object:
        return await self.service.dispatch(
            DispatchProfilerCall(
                token=self.token(scope_id),
                work=ProfilerWorkKey(purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC, focus="x"),
                request="Where does decode time go?",
                session_id=session_id,
            )
        )


def _harness(root: Path) -> _Harness:
    clock = FakeClock()
    executor = FakeEvaluationExecutor(clock, supported_evidence_kinds=("accuracy",))
    coordinator = EvaluationCoordinator(executor, InMemoryEvaluationStore(), clock)
    backend = _ContentBackend(coordinator)
    namespace = _namespace(root)
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
    stopping = [False]
    service = EvaluationAgentService(
        backend, namespace, root / "evaluation.sock", profiler, stopping=lambda: stopping[0]
    )
    return _Harness(
        service, profiler, provision, executor, backend, coordinator, namespace, stopping
    )


_Step = (
    st.tuples(st.just("submit"), st.sampled_from(_SCOPES), st.sampled_from(("x", "y", "own")))
    | st.tuples(st.just("profile"), st.sampled_from(_SCOPES), st.none())
    | st.tuples(st.just("release"), st.sampled_from(_SCOPES), st.none())
    | st.tuples(st.just("reopen"), st.sampled_from(_SCOPES), st.none())
)


@dataclass
class _Model:
    released: set[str] = field(default_factory=set)
    # handle -> canonical scope that first submitted this measurement.
    owner: dict[str, str] = field(default_factory=dict)
    # Each submitting scope has an independent live wait association.
    requesters: dict[str, set[str]] = field(default_factory=dict)
    canceled: set[str] = field(default_factory=set)
    # profiler operation -> scope
    operations: dict[str, str] = field(default_factory=dict)
    canceled_operations: set[str] = field(default_factory=set)


async def _release(harness: _Harness, model: _Model, scope: str) -> None:
    release = await harness.service.cancel_scope(scope)
    expected_evaluations = {
        handle
        for handle, requesters in model.requesters.items()
        if requesters == {scope} and handle not in model.canceled
    }
    expected_operations = {
        operation
        for operation, owner in model.operations.items()
        if owner == scope and operation not in model.canceled_operations
    }
    if scope in model.released:
        assert release == ScopeRelease(scope_id=scope, first_release=False)
    else:
        assert release.first_release
        assert len(set(release.evaluations)) == len(release.evaluations)
        assert set(release.evaluations) == expected_evaluations
        assert set(release.profiler_operations) == expected_operations
        model.canceled |= expected_evaluations
        model.canceled_operations |= expected_operations
    model.released.add(scope)
    for requesters in model.requesters.values():
        requesters.discard(scope)


async def _check_cancellations(harness: _Harness, model: _Model) -> None:
    for handle in model.owner:
        state = await harness.coordinator.status(handle)
        assert (state is EvaluationState.CANCELED) == (handle in model.canceled), handle
    for operation, scope in model.operations.items():
        record = await harness.profiler.status(operation, f"implementer:{scope}", None)
        canceled = record.operation.state is ProfilerOperationState.CANCELED
        assert canceled == (operation in model.canceled_operations), operation


async def _run_steps(harness: _Harness, steps: list[tuple[str, str, str | None]]) -> None:
    model = _Model()
    for action, scope, content in steps:
        if action == "submit":
            harness.backend.content[scope] = scope if content == "own" else str(content)
            reply = await harness.submit(scope)
            if scope in model.released:
                assert reply == ScopeReleasedReply()
            else:
                assert isinstance(reply, SubmittedReply)
                model.owner.setdefault(reply.handle_id, scope)
                model.requesters.setdefault(reply.handle_id, set()).add(scope)
        elif action == "profile":
            reply = await harness.profile(scope)
            if scope in model.released:
                assert reply == ScopeReleasedReply()
            else:
                assert isinstance(reply, ProfilerDispatchedReply)
                model.operations[reply.operation_id] = scope
        elif action == "release":
            await _release(harness, model, scope)
        else:
            await harness.service.reopen_scope(scope)
            model.released.discard(scope)
        await _check_cancellations(harness, model)
    await harness.profiler.close()


@settings(max_examples=20, deadline=None)
@example(steps=[("submit", "m-c", "x"), ("submit", "m-a", "x"), ("release", "m-c", None)])
@given(steps=st.lists(_Step, max_size=14))
def test_release_cancels_exactly_captures_without_live_requesters_once(
    steps: list[tuple[str, str, str | None]],
) -> None:
    """Any interleaving releases its waits and cancels only unobserved captures, once."""
    with tempfile.TemporaryDirectory(prefix="vs-release-") as root:
        asyncio.run(_run_steps(_harness(Path(root)), steps))


@pytest.mark.asyncio
async def test_released_scopes_stay_released_for_a_new_service_over_the_same_state(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    await harness.service.cancel_scope("m-a")
    resumed = EvaluationAgentService(
        harness.backend,
        harness.namespace,
        tmp_path / "resumed.sock",
        harness.profiler,
    )
    grant = resumed.grant(
        principal_id="implementer:m-a", role=EvaluationAgentRole.IMPLEMENTER, scope_id="m-a"
    )
    submit = SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
    assert await resumed.dispatch(submit) == ScopeReleasedReply()
    await resumed.reopen_scope("m-a")
    assert isinstance(await resumed.dispatch(submit), SubmittedReply)
    await harness.profiler.close()


@pytest.mark.asyncio
async def test_a_stop_cancels_queued_profiler_operations_before_they_start(
    tmp_path: Path,
) -> None:
    """Regression: a profile queued behind a running turn of its session ran after the stop."""
    harness = _harness(tmp_path)
    first = await harness.profile("m-a")
    assert isinstance(first, ProfilerDispatchedReply)
    await harness.provision.wait_started(first.operation_id)
    queued = await harness.profile("m-a", session_id=first.session_id)
    assert isinstance(queued, ProfilerDispatchedReply)

    harness.stopping[0] = True
    await harness.service.cancel_outstanding()
    assert await harness.profile("m-a") == RunStoppingReply()
    harness.provision.unsupported(first.operation_id, "no capture")
    await harness.profiler.await_result(first.operation_id, "implementer:m-a", None, 1.0)

    status = await harness.profiler.status(queued.operation_id, "implementer:m-a", None)
    assert status.operation.state is ProfilerOperationState.CANCELED
    assert [turn.operation_id for turn in harness.provision.turns] == [first.operation_id]
    await harness.profiler.close()
