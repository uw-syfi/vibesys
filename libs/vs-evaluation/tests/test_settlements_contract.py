"""One ownership and cancellation contract for service and in-memory settlements."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_evaluation.api import (
    EVALUATION_ACCESS_STATE_PATH,
    ContentDigest,
    EvaluationAgentState,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationDependencyError,
    EvaluationFailed,
    EvaluationPending,
    EvaluationRequest,
    EvaluationSettlementObservation,
    EvaluationSettlements,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvaluationUnknown,
    EvidenceFingerprints,
    ExecutorObservation,
    OwnedEvaluationDependencies,
    ScopeLifecycleStore,
    ServiceEvaluationSettlements,
    SettlementErrorCode,
    StageState,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements

SettlementsFixture = tuple[FakeEvaluationSettlements, EvaluationSettlements]


@pytest.fixture(params=["fake", "service"])
def settlements(request: pytest.FixtureRequest) -> SettlementsFixture:
    fake = FakeEvaluationSettlements()
    implementation = (
        fake
        if request.param == "fake"
        else ServiceEvaluationSettlements(fake.backend, fake.namespace)
    )
    return fake, implementation


async def submit(
    fake: FakeEvaluationSettlements, key: str = "work", scope: str = "scope", generation: int = 0
) -> str:
    digest = ContentDigest.sha256(b"identity")
    return await fake.submit(
        EvaluationRequest(
            key=key,
            owner_scope=scope,
            owner_generation=generation,
            stages=(EvaluationStep(name="benchmark", payload={}),),
        ),
        EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
    )


@pytest.mark.asyncio
async def test_terminal_before_wait_needs_no_external_wait(settlements: SettlementsFixture) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    fake.executor.set_state(
        handle,
        EvaluationState.SUCCEEDED,
        stage_results=(EvaluationStepResult(name="benchmark", state=StageState.SUCCEEDED),),
    )
    await fake.coordinator.status(handle)
    dependencies = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    inspections = list(fake.executor.inspections)
    result = await implementation.wait_any(dependencies)
    assert fake.executor.inspections == inspections
    assert isinstance(result[0].result, EvaluationCompleted)
    assert fake.executor.wait_calls == []
    assert await implementation.observe(dependencies) == result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [EvaluationState.FAILED, EvaluationState.CANCELED, EvaluationState.SUPERSEDED]
)
async def test_all_terminal_outcomes_are_observed(
    settlements: SettlementsFixture, state: EvaluationState
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    fake.executor.set_state(
        handle, state, failure="failed" if state is EvaluationState.FAILED else None
    )
    await fake.coordinator.status(handle)
    result = await implementation.wait_any(
        OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    )
    assert isinstance(
        result[0].result,
        EvaluationFailed if state is EvaluationState.FAILED else EvaluationCanceled,
    )


@pytest.mark.asyncio
async def test_cancel_observer_preserves_owned_job(settlements: SettlementsFixture) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    dependencies = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    waiter = asyncio.create_task(implementation.wait_any(dependencies))
    await fake.executor.wait_started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert fake.executor.cancellations == []
    assert isinstance((await implementation.observe(dependencies))[0].result, EvaluationPending)
    fake.executor.set_state(handle, EvaluationState.CANCELED)
    result = await implementation.wait_any(dependencies)
    assert isinstance(result[0].result, EvaluationCanceled)


@pytest.mark.asyncio
async def test_wait_any_returns_one_settlement_without_waiting_for_all(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    first, second = await submit(fake, "first"), await submit(fake, "second")
    dependencies = OwnedEvaluationDependencies(
        scope_id="scope", generation=0, handles=(first, second)
    )
    waiter = asyncio.create_task(implementation.wait_any(dependencies))
    await fake.executor.wait_started.wait()
    fake.executor.set_state(second, EvaluationState.CANCELED)
    result = await waiter
    assert [item.handle_id for item in result] == [second]
    assert fake.executor.cancellations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "generation", "code"),
    [("other", 0, SettlementErrorCode.UNOWNED), ("scope", 1, SettlementErrorCode.STALE_GENERATION)],
)
async def test_foreign_or_stale_dependencies_rejected(
    settlements: SettlementsFixture, scope: str, generation: int, code: SettlementErrorCode
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    with pytest.raises(EvaluationDependencyError) as error:
        await implementation.observe(
            OwnedEvaluationDependencies(scope_id=scope, generation=generation, handles=(handle,))
        )
    assert error.value.code is code
    assert fake.executor.wait_calls == []


@given(
    st.lists(
        st.text(min_size=1).filter(lambda value: value.strip() == value), min_size=1, max_size=6
    )
)
def test_dependency_validation_matches_identity_uniqueness(handles: list[str]) -> None:
    if len(handles) != len(set(handles)):
        with pytest.raises(ValidationError):
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=tuple(handles))
    else:
        assert OwnedEvaluationDependencies(
            scope_id="scope", generation=0, handles=tuple(handles)
        ).handles == tuple(handles)


@pytest.mark.asyncio
async def test_host_waits_renew_past_agent_and_coordinator_bounds(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    fake.executor.script_wait_timeout(45.0, count=8)
    fake.executor.script_wait_transition(
        ExecutorObservation(state=EvaluationState.CANCELED), elapsed_s=0.0
    )
    result = await implementation.wait_any(
        OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    )
    assert isinstance(result[0].result, EvaluationCanceled)
    assert fake.executor.clock.monotonic() == 360.0
    assert all(timeout == 45.0 for _, timeout in fake.executor.wait_calls)
    assert fake.executor.cancellations == []


@pytest.mark.asyncio
async def test_transport_loss_stays_unknown(settlements: SettlementsFixture) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    fake.executor.timeout_next_inspections()
    result = await implementation.wait_any(
        OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    )
    assert isinstance(result[0].result, EvaluationUnknown)
    assert fake.executor.cancellations == []


@pytest.mark.asyncio
async def test_reopened_scope_fences_old_owned_handle(settlements: SettlementsFixture) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    scopes = ScopeLifecycleStore(fake.namespace)
    scopes.begin("scope")
    scopes.complete("scope")
    scopes.reopen("scope")
    with pytest.raises(EvaluationDependencyError) as error:
        await implementation.observe(
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
        )
    assert error.value.code is SettlementErrorCode.STALE_GENERATION


@pytest.mark.asyncio
async def test_unknown_identity_rejected_before_wait(settlements: SettlementsFixture) -> None:
    fake, implementation = settlements
    with pytest.raises(EvaluationDependencyError) as error:
        await implementation.wait_any(
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=("absent",))
        )
    assert error.value.code is SettlementErrorCode.UNKNOWN_HANDLE
    assert fake.executor.wait_calls == []


@pytest.mark.asyncio
async def test_duplicate_submission_preserves_one_immutable_dependency(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    first, second = await submit(fake), await submit(fake)
    assert first == second
    observations = await implementation.observe(
        OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(first,))
    )
    assert len(observations) == 1
    assert len(fake.executor.submissions) == 1
    restarted = ServiceEvaluationSettlements(fake.backend, fake.namespace)
    assert (
        await restarted.observe(
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(first,))
        )
        == observations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["record", "ownership"])
async def test_durable_read_loss_is_unknown(settlements: SettlementsFixture, source: str) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    if source == "record":
        fake.backend.read_error = OSError("record unavailable")
    else:
        fake.backend.ownership_error = OSError("ownership unavailable")
    observations = await implementation.observe(
        OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    )
    assert isinstance(observations[0].result, EvaluationUnknown)
    assert observations[0].revision is None
    assert fake.executor.wait_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["candidate", "evaluator", "workload", "environment"])
@pytest.mark.parametrize("terminal", [False, True])
async def test_corrupt_access_cannot_replace_submitted_identity(
    settlements: SettlementsFixture, field: str, *, terminal: bool
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    if terminal:
        fake.executor.set_state(handle, EvaluationState.CANCELED)
        await fake.coordinator.status(handle)
    state = fake.namespace.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
    access = state.handles[0]
    corrupt = access.fingerprints.model_copy(update={field: ContentDigest.sha256(b"other")})
    fake.namespace.save(
        EVALUATION_ACCESS_STATE_PATH,
        state.model_copy(
            update={"handles": (access.model_copy(update={"fingerprints": corrupt}),)}
        ),
    )
    with pytest.raises(EvaluationDependencyError) as error:
        await implementation.observe(
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
        )
    assert error.value.code is SettlementErrorCode.IDENTITY_CONFLICT
    assert fake.executor.wait_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_missing_legacy_identity_stays_unknown(
    settlements: SettlementsFixture, *, terminal: bool
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    if terminal:
        fake.executor.set_state(handle, EvaluationState.CANCELED)
        await fake.coordinator.status(handle)
    fake.backend.forget_submission(handle)
    result = await implementation.wait_any(
        OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    )
    assert isinstance(result[0].result, EvaluationUnknown)
    assert fake.executor.wait_calls == []


@pytest.mark.asyncio
async def test_reopen_during_observation_cannot_return_old_generation(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    gate = fake.backend.hold_record_reads()
    observation = asyncio.create_task(
        implementation.observe(
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
        )
    )
    await fake.backend.record_read_started.wait()
    scopes = ScopeLifecycleStore(fake.namespace)
    scopes.begin("scope")
    scopes.complete("scope")
    scopes.reopen("scope")
    gate.set()
    with pytest.raises(EvaluationDependencyError) as error:
        await observation
    assert error.value.code is SettlementErrorCode.STALE_GENERATION


@pytest.mark.asyncio
async def test_withdrawal_during_observation_cannot_return_detached_dependency(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    gate = fake.backend.hold_record_reads()
    observation = asyncio.create_task(
        implementation.observe(
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
        )
    )
    await fake.backend.record_read_started.wait()
    # The public pure transition is the same durable intent CancelCall commits,
    # before physical cancellation. Keep generation unchanged to test withdrawal.
    state = fake.namespace.load(EVALUATION_ACCESS_STATE_PATH, EvaluationAgentState)
    fake.namespace.save(
        EVALUATION_ACCESS_STATE_PATH,
        state.model_copy(update={"handles": (state.handles[0].detach(scope_id="scope"),)}),
    )
    gate.set()
    with pytest.raises(EvaluationDependencyError) as error:
        await observation
    assert error.value.code is SettlementErrorCode.UNOWNED


@pytest.mark.asyncio
async def test_wrong_handle_record_is_a_typed_identity_conflict(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    first, second = await submit(fake, "first"), await submit(fake, "second")
    fake.backend.misroute_next_record_read(second)
    with pytest.raises(EvaluationDependencyError) as error:
        await implementation.observe(
            OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(first,))
        )
    assert error.value.code is SettlementErrorCode.IDENTITY_CONFLICT
    assert error.value.handle_id == first
    assert fake.executor.wait_calls == []


@given(
    outcome=st.sampled_from(["pending", "unknown", "completed", "failed", "canceled"]),
    has_revision=st.booleans(),
    matching_handle=st.booleans(),
)
def test_observation_revision_and_handle_attribution_are_consistent(
    outcome: str, *, has_revision: bool, matching_handle: bool
) -> None:
    digest = ContentDigest.sha256(b"identity")
    inner_handle = "outer" if matching_handle else "other"
    results = {
        "pending": EvaluationPending(state=EvaluationState.QUEUED),
        "unknown": EvaluationUnknown(detail="not observed"),
        "completed": EvaluationCompleted(handle_id=inner_handle, stages=()),
        "failed": EvaluationFailed(handle_id=inner_handle, message="failed"),
        "canceled": EvaluationCanceled(handle_id=inner_handle, state=EvaluationState.CANCELED),
    }
    valid = (has_revision or outcome == "unknown") and (
        outcome in {"pending", "unknown"} or matching_handle
    )
    values = {
        "handle_id": "outer",
        "scope_id": "scope",
        "generation": 0,
        "fingerprints": EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
        "revision": 1 if has_revision else None,
        "result": results[outcome],
    }
    if valid:
        assert EvaluationSettlementObservation.model_validate(values).result == results[outcome]
    else:
        with pytest.raises(ValidationError):
            EvaluationSettlementObservation.model_validate(values)


@pytest.mark.asyncio
async def test_optional_scheduler_evidence_agrees_across_implementations(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    dependency = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    observation = (await implementation.observe(dependency))[0]
    assert observation.pending_reason is None
    assert observation.estimated_start_s is None
    assert observation.queued_seconds is None
    assert observation.ran_seconds is None
    enriched = EvaluationSettlementObservation.model_validate(
        {
            **observation.model_dump(),
            "pending_reason": "Resources",
            "estimated_start_s": 1234.0,
            "stage": "queued",
            "queued_seconds": 42.0,
        }
    )
    assert (
        EvaluationSettlementObservation.model_validate_json(enriched.model_dump_json()) == enriched
    )


@pytest.mark.parametrize("field", ["estimated_start_s", "queued_seconds", "ran_seconds"])
@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_scheduler_times_reject_invalid_values(
    settlements: SettlementsFixture, field: str, value: float
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    dependency = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    observation = (await implementation.observe(dependency))[0]
    with pytest.raises(ValidationError, match=field):
        EvaluationSettlementObservation.model_validate({**observation.model_dump(), field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", [EvaluationState.QUEUED, EvaluationState.STARTING, EvaluationState.RUNNING]
)
async def test_inspect_pending_evaluation_remains_read_only_after_observer_restart(
    settlements: SettlementsFixture,
    state: Literal[EvaluationState.QUEUED, EvaluationState.STARTING, EvaluationState.RUNNING],
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    dependency = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    original = (await implementation.observe(dependency))[0]
    fake.executor.set_state(handle, state)
    submissions = list(fake.executor.submissions)
    inspections = len(fake.executor.inspections)

    pending = (await implementation.inspect(dependency))[0]
    assert pending.result == EvaluationPending(state=state)
    assert pending.fingerprints == original.fingerprints
    assert (pending.handle_id, pending.scope_id, pending.generation) == (handle, "scope", 0)
    assert len(fake.executor.inspections) == inspections + 1

    restarted = ServiceEvaluationSettlements(fake.backend, fake.namespace)
    assert (await restarted.inspect(dependency))[0] == pending
    fake.executor.set_state(
        handle,
        EvaluationState.SUCCEEDED,
        stage_results=(EvaluationStepResult(name="benchmark", state=StageState.SUCCEEDED),),
    )
    completed = (await restarted.inspect(dependency))[0]
    assert isinstance(completed.result, EvaluationCompleted)
    assert completed.fingerprints == pending.fingerprints
    assert fake.executor.submissions == submissions
    assert fake.executor.cancellations == []
    assert fake.executor.wait_calls == []


@pytest.mark.asyncio
async def test_inspect_refreshes_external_result_without_dispatch_or_cancel(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    fake.executor.set_state(handle, EvaluationState.FAILED, failure="external execution failed")
    submissions = list(fake.executor.submissions)
    cancellations = list(fake.executor.cancellations)
    inspections = len(fake.executor.inspections)
    dependency = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    result = (await implementation.inspect(dependency))[0]
    assert isinstance(result.result, EvaluationFailed)
    assert fake.executor.submissions == submissions
    assert fake.executor.cancellations == cancellations
    assert len(fake.executor.inspections) == inspections + 1


@pytest.mark.asyncio
async def test_inspect_absent_external_identity_is_unknown_without_resubmission(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    fake.executor.script_observations(None)
    submissions = list(fake.executor.submissions)
    dependency = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    result = (await implementation.inspect(dependency))[0]
    assert isinstance(result.result, EvaluationUnknown)
    assert fake.executor.submissions == submissions
    assert fake.executor.cancellations == []


@pytest.mark.asyncio
async def test_inspect_validates_all_owners_before_external_inspection(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    owned = await submit(fake)
    other = await submit(fake, key="other", scope="another")
    inspections = list(fake.executor.inspections)
    dependency = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(owned, other))
    with pytest.raises(EvaluationDependencyError) as error:
        await implementation.inspect(dependency)
    assert error.value.code is SettlementErrorCode.UNOWNED
    assert fake.executor.inspections == inspections


@pytest.mark.asyncio
async def test_inspect_requires_available_submitted_provenance(
    settlements: SettlementsFixture,
) -> None:
    fake, implementation = settlements
    handle = await submit(fake)
    fake.backend.forget_submission(handle)
    inspections = list(fake.executor.inspections)
    dependency = OwnedEvaluationDependencies(scope_id="scope", generation=0, handles=(handle,))
    result = (await implementation.inspect(dependency))[0]
    assert isinstance(result.result, EvaluationUnknown)
    assert fake.executor.inspections == inspections


@pytest.mark.asyncio
async def test_coordinator_inspection_does_not_dispatch_prepared_work() -> None:
    fake = FakeEvaluationSettlements()
    request = EvaluationRequest(
        key="prepared-only",
        owner_scope="scope",
        stages=(EvaluationStep(name="benchmark", payload={}),),
    )
    handle = await fake.coordinator.prepare(request)
    assert await fake.coordinator.inspect_snapshot(handle.id) is None
    assert fake.executor.submissions == []
    assert fake.executor.cancellations == []
    assert (await fake.coordinator.recorded_snapshot(handle.id)).submission_pending
    fake.executor.script_observations(ExecutorObservation(state=EvaluationState.QUEUED))
    assert await fake.coordinator.inspect_snapshot(handle.id) is None
    assert (await fake.coordinator.recorded_snapshot(handle.id)).submission_pending
    assert fake.executor.submissions == []


@pytest.mark.asyncio
async def test_coordinator_inspection_does_not_retry_durable_cancellation() -> None:
    fake = FakeEvaluationSettlements()
    handle = await submit(fake)
    record = await fake.coordinator.recorded_snapshot(handle)
    await fake.store.compare_and_set(
        record.model_copy(update={"cancel_requested": True, "revision": record.revision + 1}),
        expected_revision=record.revision,
    )
    refreshed = await fake.coordinator.inspect_snapshot(handle)
    assert refreshed is not None
    assert refreshed.cancel_requested
    assert fake.executor.cancellations == []
