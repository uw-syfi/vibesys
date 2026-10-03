"""Public contract tests for asynchronous profiler-agent conversations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vs_async_ops.api import OperationPolicy
from vs_async_ops.api.testing import TimeoutOnceWaiter
from vs_evaluation.api import (
    MAX_PROFILER_NARRATIVE_CHARS,
    MAX_PROFILER_REQUEST_CHARS,
    PROFILER_TERMINAL_RETENTION,
    ContentDigest,
    DispatchProfilerCall,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ProfilerAgentAccessError,
    ProfilerAgentResult,
    ProfilerAgentService,
    ProfilerAgentServiceHooks,
    ProfilerAgentUnavailableError,
    ProfilerIdempotencyConflictError,
    ProfilerOperationState,
    ProfilerResultOutcome,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    TrustedEvidence,
)
from vs_evaluation.api.testing import FakeProfilerTurnProvision
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    RunExecutionRecord,
    StateNamespace,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


async def _resolve_no_evidence(
    principal_id: str,
    scope_id: str | None,
    candidate_snapshot_id: str,
    evidence_ids: tuple[str, ...],
) -> tuple[TrustedEvidence, ...]:
    del principal_id, scope_id, candidate_snapshot_id
    if evidence_ids:
        raise _UnknownEvidenceError
    return ()


class _UnknownEvidenceError(ValueError):
    def __init__(self) -> None:
        super().__init__("unknown trusted profile evidence")


class _ObserverUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("observer unavailable")


@dataclass(frozen=True, slots=True)
class _ServiceOptions:
    timeout_immediately: bool = False
    max_in_flight: int | None = None
    events: Callable[[object], None] | None = None
    terminal_retention: int = PROFILER_TERMINAL_RETENTION


_DEFAULT_SERVICE_OPTIONS = _ServiceOptions()
_WORK = ProfilerWorkKey(
    purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC,
    focus="focused test diagnostic",
)


def _trusted_evidence(kind: EvidenceKind = EvidenceKind.PROFILE) -> TrustedEvidence:
    fingerprints = EvidenceFingerprints(
        candidate=ContentDigest.sha256(b"candidate"),
        evaluator=ContentDigest.sha256(b"profiler"),
        workload=ContentDigest.sha256(b"workload"),
        environment=ContentDigest.sha256(b"environment"),
    )
    trusted_inputs = ContentDigest.sha256(b"inputs")
    return TrustedEvidence(
        evidence_id="a" * 64,
        evaluation_id="evaluation",
        stage_name=kind.value,
        kind=kind,
        fingerprints=fingerprints,
        trusted_inputs=trusted_inputs,
        outcome=EvidenceOutcome.OBSERVED,
        accepted_round=0,
    )


def _namespace(tmp_path: Path) -> StateNamespace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = Project.open(tmp_path)
    project.state.create_project("test")
    run_id = "profiler-agent-test"
    if project.state.current_run_id() == run_id:
        return project.state.local_namespace(run_id, "profiler-agent")
    manifest = project.state.new_run_manifest(
        "Profiler agent test",
        run_id=run_id,
        trusted_input_baseline="a" * 40,
        branch="test/profiler-agent",
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
    return project.state.local_namespace(manifest.run_id, "profiler-agent")


def test_profiler_payloads_have_explicit_size_bounds() -> None:
    capability = "x" * 32
    with pytest.raises(ValidationError, match="at most 16384 characters"):
        DispatchProfilerCall(
            token=capability,
            work=_WORK,
            request="x" * (MAX_PROFILER_REQUEST_CHARS + 1),
        )
    with pytest.raises(ValidationError, match="at most 65536 characters"):
        ProfilerAgentResult(
            outcome=ProfilerResultOutcome.OBSERVED,
            narrative="x" * (MAX_PROFILER_NARRATIVE_CHARS + 1),
        )


def _service(
    tmp_path: Path,
    provision: FakeProfilerTurnProvision | None,
    *,
    options: _ServiceOptions = _DEFAULT_SERVICE_OPTIONS,
    resolve_evidence: Callable[
        [str, str | None, str, tuple[str, ...]],
        Awaitable[tuple[TrustedEvidence, ...]],
    ] = _resolve_no_evidence,
) -> ProfilerAgentService:
    async def candidate_snapshot(scope: str | None) -> str:
        return f"snapshot:{scope}"

    return ProfilerAgentService(
        provision,
        _namespace(tmp_path),
        ProfilerAgentServiceHooks(
            candidate_snapshot=candidate_snapshot,
            resolve_evidence=resolve_evidence,
            waiter=TimeoutOnceWaiter() if options.timeout_immediately else None,
            events=options.events,
        ),
        OperationPolicy(max_in_flight=options.max_in_flight),
        terminal_retention=options.terminal_retention,
    )


@pytest.mark.asyncio
async def test_dispatch_is_nonblocking_and_timeout_is_observational(tmp_path: Path) -> None:
    provision = FakeProfilerTurnProvision()
    evidence = _trusted_evidence()
    queries: list[tuple[str, str | None, str, tuple[str, ...]]] = []

    async def resolve(
        principal_id: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
        evidence_ids: tuple[str, ...],
    ) -> tuple[TrustedEvidence, ...]:
        queries.append((principal_id, scope_id, candidate_snapshot_id, evidence_ids))
        return (evidence,) if evidence_ids == (evidence.evidence_id,) else ()

    service = _service(
        tmp_path,
        provision,
        options=_ServiceOptions(timeout_immediately=True),
        resolve_evidence=resolve,
    )

    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Measure the prefill bottleneck.",
        work=_WORK,
        session_id=None,
    )
    observed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)

    assert observed.timed_out is True
    assert observed.operation.state in {
        ProfilerOperationState.QUEUED,
        ProfilerOperationState.RUNNING,
    }
    await provision.wait_started(dispatched.operation_id)
    provision.complete(dispatched.operation_id, evidence_ids=(evidence.evidence_id,))
    completed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)
    assert completed.operation.result is not None
    assert completed.operation.result.report.evidence_ids == (evidence.evidence_id,)
    assert completed.operation.result.trusted_evidence == (evidence,)
    assert queries == [("implementer", "candidate", "snapshot:candidate", (evidence.evidence_id,))]
    run_projection = await service.project_run()
    assert run_projection[0].evidence_recorded
    projection = await service.project_candidate("snapshot:candidate")
    assert projection.completed[0].result.trusted_evidence == (evidence,)
    assert (await service.project_candidate("snapshot:other")).completed == ()


@pytest.mark.asyncio
async def test_run_projection_preserves_profiler_request_identity_and_trust_state(
    tmp_path: Path,
) -> None:
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision)
    dispatched = await service.dispatch(
        principal_id="implementer:hypothesis-prefill",
        scope_id="candidate-prefill",
        request="Measure whether prefill attention saturates memory bandwidth.",
        work=_WORK,
        session_id=None,
    )

    active = (await service.project_run())[0]
    assert active.principal_id == "implementer:hypothesis-prefill"
    assert active.scope_id == "candidate-prefill"
    assert active.request == "Measure whether prefill attention saturates memory bandwidth."
    assert active.candidate_snapshot_id == "snapshot:candidate-prefill"
    assert not active.evidence_recorded

    await provision.wait_started(dispatched.operation_id)
    provision.complete(dispatched.operation_id)
    await service.await_result(
        dispatched.operation_id,
        "implementer:hypothesis-prefill",
        "candidate-prefill",
        10,
    )
    completed = (await service.project_run())[0]
    assert completed.state is ProfilerOperationState.COMPLETED
    assert not completed.evidence_recorded
    assert completed.outcome is ProfilerResultOutcome.OBSERVED

    unsupported = await service.dispatch(
        principal_id="implementer:hypothesis-prefill",
        scope_id="candidate-prefill",
        request="Measure an unavailable hardware counter.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(unsupported.operation_id)
    provision.unsupported(unsupported.operation_id, "counter is unavailable")
    await service.await_result(
        unsupported.operation_id,
        "implementer:hypothesis-prefill",
        "candidate-prefill",
        10,
    )
    unsupported_observation = (await service.project_run())[-1]
    assert not unsupported_observation.evidence_recorded
    assert unsupported_observation.outcome is ProfilerResultOutcome.UNSUPPORTED


@pytest.mark.asyncio
async def test_candidate_projection_exposes_only_matching_active_and_completed_work(
    tmp_path: Path,
) -> None:
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, options=_ServiceOptions(max_in_flight=2))
    completed = await service.dispatch(
        principal_id="first-implementer",
        scope_id="candidate",
        request="Measure the dominant path.",
        work=_WORK,
        session_id=None,
    )
    active = await service.dispatch(
        principal_id="second-implementer",
        scope_id="candidate",
        request="Investigate a competing cause.",
        work=_WORK,
        session_id=None,
    )
    foreign = await service.dispatch(
        principal_id="third-implementer",
        scope_id="other",
        request="Profile another candidate.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(completed.operation_id)
    await provision.wait_started(active.operation_id)
    provision.complete(completed.operation_id)
    await service.await_result(completed.operation_id, "first-implementer", "candidate", 10)
    await provision.wait_started(foreign.operation_id)

    projection = await service.project_candidate("snapshot:candidate")

    assert projection.candidate_snapshot_id == "snapshot:candidate"
    assert [item.operation_id for item in projection.in_flight] == [active.operation_id]
    assert projection.in_flight[0].work == _WORK
    assert [item.operation_id for item in projection.completed] == [completed.operation_id]
    assert projection.completed[0].work == _WORK
    assert projection.completed[0].result.report.narrative == "advisory profile interpretation"
    assert foreign.operation_id not in {item.operation_id for item in projection.in_flight}
    provision.complete(active.operation_id)
    provision.complete(foreign.operation_id)
    await asyncio.gather(
        service.await_result(active.operation_id, "second-implementer", "candidate", 10),
        service.await_result(foreign.operation_id, "third-implementer", "other", 10),
    )


@pytest.mark.asyncio
async def test_resume_serializes_turns_and_other_sessions_can_run(tmp_path: Path) -> None:
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, options=_ServiceOptions(max_in_flight=2))
    first = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Find the dominant kernel.",
        work=_WORK,
        session_id=None,
    )
    second = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Compare its launch variants.",
        work=_WORK,
        session_id=first.session_id,
    )
    other = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Investigate memory traffic independently.",
        work=_WORK,
        session_id=None,
    )

    await provision.wait_started(first.operation_id)
    await provision.wait_started(other.operation_id)
    assert second.operation_id not in {item.operation_id for item in provision.turns}
    assert provision.max_active == 2

    provision.complete(first.operation_id)
    await provision.wait_started(second.operation_id)
    assert provision.turns[-1].session_id == first.session_id
    assert provision.turns[-1].candidate_snapshot_id == "snapshot:candidate"
    provision.complete(second.operation_id)
    provision.complete(other.operation_id)
    await asyncio.gather(
        service.await_result(second.operation_id, "implementer", "candidate", 10),
        service.await_result(other.operation_id, "implementer", "candidate", 10),
    )


@pytest.mark.asyncio
async def test_cancel_scope_provision_restart_and_absence(tmp_path: Path) -> None:
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision)
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Capture a trace.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)
    canceled = await service.cancel(dispatched.operation_id, "implementer", "candidate")
    assert canceled.operation.state is ProfilerOperationState.CANCELED
    observed_from_new_workspace = await service.status(
        dispatched.operation_id, "implementer", "other"
    )
    assert observed_from_new_workspace.operation.state is ProfilerOperationState.CANCELED
    with pytest.raises(ProfilerAgentAccessError, match="another scope"):
        await service.status(dispatched.operation_id, "another-implementer", "candidate")

    live = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Capture another trace.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(live.operation_id)
    restarted = _service(tmp_path, FakeProfilerTurnProvision())
    interrupted = await restarted.status(live.operation_id, "implementer", "candidate")
    assert interrupted.operation.state is ProfilerOperationState.INTERRUPTED
    provision.complete(live.operation_id)

    scoped = await service.dispatch(
        principal_id="implementer",
        scope_id="retired-candidate",
        request="Capture on a disposable candidate.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(scoped.operation_id)
    await service.cancel_scope("retired-candidate")
    retired = await service.status(scoped.operation_id, "implementer", "retired-candidate")
    assert retired.operation.state is ProfilerOperationState.CANCELED
    assert provision.canceled_scopes == ["retired-candidate"]

    changed = _service(tmp_path, FakeProfilerTurnProvision("fake-profiler-v2"))
    with pytest.raises(ProfilerAgentAccessError, match="different provision"):
        await changed.dispatch(
            principal_id="implementer",
            scope_id="candidate",
            request="Continue the analysis.",
            work=_WORK,
            session_id=dispatched.session_id,
        )

    absent = _service(tmp_path / "absent", None)
    with pytest.raises(ProfilerAgentUnavailableError):
        await absent.dispatch(
            principal_id="implementer",
            scope_id="candidate",
            request="Profile this.",
            work=_WORK,
            session_id=None,
        )


@pytest.mark.asyncio
async def test_terminal_retention_compacts_old_operation_files(tmp_path: Path) -> None:
    retention = 3
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, options=_ServiceOptions(terminal_retention=retention))
    operation_ids: list[str] = []

    for index in range(retention + 1):
        dispatched = await service.dispatch(
            principal_id="implementer",
            scope_id="candidate",
            request=f"Inspect bounded gap {index}.",
            work=_WORK,
            session_id=None,
        )
        operation_ids.append(dispatched.operation_id)
        await provision.wait_started(dispatched.operation_id)
        provision.complete(dispatched.operation_id)
        completed = await service.await_result(
            dispatched.operation_id, "implementer", "candidate", 10
        )
        assert completed.operation.state is ProfilerOperationState.COMPLETED

    operation_dir = (
        Project.open(tmp_path)
        .state.local_namespace("profiler-agent-test", "profiler-agent")
        .external_directory()
        / "profiler-agent-operations"
    )
    operation_files = {path.stem for path in operation_dir.glob("*.json")} - {"index"}
    assert len(operation_files) == retention
    assert operation_ids[0] not in operation_files
    assert set(operation_ids[1:]) == operation_files


def test_default_terminal_retention_is_pinned() -> None:
    assert PROFILER_TERMINAL_RETENTION == 128


@pytest.mark.parametrize("retention", [0, -1])
def test_a_terminal_retention_below_one_is_rejected(tmp_path: Path, retention: int) -> None:
    with pytest.raises(ValueError, match="terminal_retention"):
        _service(tmp_path, None, options=_ServiceOptions(terminal_retention=retention))


@pytest.mark.asyncio
async def test_unknown_evidence_fails_before_advisory_result_is_exposed(tmp_path: Path) -> None:
    async def reject(
        principal_id: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
        evidence_ids: tuple[str, ...],
    ) -> tuple[TrustedEvidence, ...]:
        del principal_id, scope_id, candidate_snapshot_id
        if evidence_ids:
            raise _UnknownEvidenceError
        return ()

    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, resolve_evidence=reject)
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Measure the bottleneck.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)
    provision.complete(dispatched.operation_id, evidence_ids=("a" * 64,))

    completed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)
    assert completed.operation.state is ProfilerOperationState.FAILED
    assert completed.operation.result is None
    assert completed.operation.error is not None
    assert "unknown trusted profile evidence" in completed.operation.error


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", ["missing", "non-profile"])
async def test_resolver_must_return_exact_profile_evidence(tmp_path: Path, resolution: str) -> None:
    profile = _trusted_evidence()
    benchmark = _trusted_evidence(EvidenceKind.BENCHMARK)

    async def resolve(
        principal_id: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
        evidence_ids: tuple[str, ...],
    ) -> tuple[TrustedEvidence, ...]:
        del principal_id, scope_id, candidate_snapshot_id, evidence_ids
        return () if resolution == "missing" else (benchmark,)

    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, resolve_evidence=resolve)
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Measure the bottleneck.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)
    requested = profile.evidence_id if resolution == "missing" else benchmark.evidence_id
    provision.complete(dispatched.operation_id, evidence_ids=(requested,))

    completed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)
    assert completed.operation.state is ProfilerOperationState.FAILED
    assert completed.operation.result is None
    assert completed.operation.error is not None
    assert "trusted profile evidence" in completed.operation.error


@pytest.mark.asyncio
async def test_request_specific_unsupported_is_a_successful_turn(tmp_path: Path) -> None:
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision)
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Collect an unavailable counter.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)
    provision.unsupported(dispatched.operation_id, "counter is absent on this device")
    completed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)

    assert completed.operation.state is ProfilerOperationState.COMPLETED
    assert completed.operation.result is not None
    assert completed.operation.result.report.outcome is ProfilerResultOutcome.UNSUPPORTED
    assert (
        completed.operation.result.report.unsupported_reason == "counter is absent on this device"
    )
    assert completed.operation.result.trusted_evidence == ()


def _resolving(
    *evidence: TrustedEvidence,
) -> Callable[[str, str | None, str, tuple[str, ...]], Awaitable[tuple[TrustedEvidence, ...]]]:
    async def resolve(
        principal_id: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
        evidence_ids: tuple[str, ...],
    ) -> tuple[TrustedEvidence, ...]:
        del principal_id, scope_id, candidate_snapshot_id
        by_id = {item.evidence_id: item for item in evidence}
        return tuple(by_id[evidence_id] for evidence_id in evidence_ids)

    return resolve


@pytest.mark.asyncio
async def test_an_unsupported_turn_may_cite_the_evidence_it_examined(tmp_path: Path) -> None:
    """Regression (r18): citing the examined capture cost two correction turns."""
    examined = _trusted_evidence().model_copy(update={"outcome": EvidenceOutcome.FAILED})
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, resolve_evidence=_resolving(examined))
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Attribute serving time.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)
    provision.unsupported(
        dispatched.operation_id,
        "the workload failed its preflight",
        evidence_ids=(examined.evidence_id,),
    )
    completed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)

    assert completed.operation.state is ProfilerOperationState.COMPLETED
    assert completed.operation.result is not None
    assert completed.operation.result.report.outcome is ProfilerResultOutcome.UNSUPPORTED
    assert completed.operation.result.trusted_evidence == (examined,)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", list(EvidenceOutcome))
async def test_an_observed_turn_cannot_rest_on_a_failed_capture(
    tmp_path: Path, outcome: EvidenceOutcome
) -> None:
    cited = _trusted_evidence().model_copy(update={"outcome": outcome})
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, resolve_evidence=_resolving(cited))
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Attribute serving time.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)
    provision.complete(dispatched.operation_id, evidence_ids=(cited.evidence_id,))
    completed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)

    if outcome is EvidenceOutcome.FAILED:
        assert completed.operation.state is ProfilerOperationState.FAILED
        assert completed.operation.error is not None
        assert "observed profile cited failed profile evidence" in completed.operation.error
    else:
        assert completed.operation.state is ProfilerOperationState.COMPLETED


@pytest.mark.asyncio
async def test_dispatch_idempotency_deduplicates_retry_and_rejects_changed_work(
    tmp_path: Path,
) -> None:
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision)
    first = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Capture one trace.",
        work=_WORK,
        session_id=None,
        idempotency_key="trace-request-1",
    )
    retry = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Capture one trace.",
        work=_WORK,
        session_id=None,
        idempotency_key="trace-request-1",
    )

    assert retry == first
    await provision.wait_started(first.operation_id)
    assert len(provision.turns) == 1
    with pytest.raises(ProfilerIdempotencyConflictError):
        await service.dispatch(
            principal_id="implementer",
            scope_id="candidate",
            request="Capture a different trace.",
            work=_WORK,
            session_id=None,
            idempotency_key="trace-request-1",
        )
    with pytest.raises(ProfilerIdempotencyConflictError):
        await service.dispatch(
            principal_id="implementer",
            scope_id="candidate",
            request="Capture one trace.",
            work=ProfilerWorkKey(
                purpose=ProfilerWorkPurpose.PLANNING_GUIDANCE,
                focus=_WORK.focus,
            ),
            session_id=None,
            idempotency_key="trace-request-1",
        )
    provision.complete(first.operation_id)
    await service.await_result(first.operation_id, "implementer", "candidate", 10)


@pytest.mark.asyncio
async def test_lifecycle_observer_failure_cannot_strand_profiler_work(tmp_path: Path) -> None:
    observed_calls = 0

    def fail_observation(_event: object) -> None:
        nonlocal observed_calls
        observed_calls += 1
        raise _ObserverUnavailableError

    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision, options=_ServiceOptions(events=fail_observation))
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Capture one trace.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)
    provision.complete(dispatched.operation_id)

    completed = await service.await_result(dispatched.operation_id, "implementer", "candidate", 10)

    assert observed_calls >= 2
    assert completed.operation.state is ProfilerOperationState.COMPLETED
    assert completed.operation.result is not None


@pytest.mark.asyncio
async def test_close_interrupts_hung_provider_before_returning(tmp_path: Path) -> None:
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision)
    dispatched = await service.dispatch(
        principal_id="implementer",
        scope_id="candidate",
        request="Capture until shutdown.",
        work=_WORK,
        session_id=None,
    )
    await provision.wait_started(dispatched.operation_id)

    await service.close()
    interrupted = await service.status(dispatched.operation_id, "implementer", "candidate")

    assert provision.canceled == [dispatched.operation_id]
    assert interrupted.operation.state is ProfilerOperationState.INTERRUPTED


@pytest.mark.asyncio
async def test_dispatch_profiles_a_named_revision_instead_of_the_scope_snapshot(
    tmp_path: Path,
) -> None:
    """Policy profiles an existing candidate revision; no live workspace is snapshotted."""
    provision = FakeProfilerTurnProvision()
    service = _service(tmp_path, provision)

    dispatched = await service.dispatch(
        principal_id="profile-a",
        scope_id=None,
        request="Where does candidate a spend its time?",
        work=_WORK,
        session_id=None,
        candidate_snapshot_id="rev-a",
    )
    await provision.wait_started(dispatched.operation_id)
    provision.complete(dispatched.operation_id)
    completed = await service.await_result(dispatched.operation_id, "profile-a", None, 10)

    assert completed.operation.candidate_snapshot_id == "rev-a"
    assert completed.operation.state is ProfilerOperationState.COMPLETED
    (observed,) = await service.project_run()
    assert (observed.principal_id, observed.candidate_snapshot_id) == ("profile-a", "rev-a")
