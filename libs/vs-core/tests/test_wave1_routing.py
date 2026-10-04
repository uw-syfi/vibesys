"""Every closed lifecycle event has exactly its declared dispatch target."""

from collections import Counter
from types import UnionType
from typing import Annotated, TypeAliasType, cast, get_args, get_origin

import pytest
from pydantic import BaseModel

import vs_core.api as core


def variants(annotation: object) -> tuple[type[BaseModel], ...]:
    """Enumerate public event contracts, including nested named unions."""
    if isinstance(annotation, TypeAliasType):
        return variants(annotation.__value__)
    if get_origin(annotation) is Annotated:
        return variants(get_args(annotation)[0])
    if get_origin(annotation) is UnionType:
        return tuple(model for member in get_args(annotation) for model in variants(member))
    assert isinstance(annotation, type)
    assert issubclass(annotation, BaseModel)
    return (annotation,)


EXPECTED_TARGET = {
    tag: target
    for target, tags in (
        (
            "vs_core._attempt_acquisition.advance",
            (
                "attempt_admitted",
                "attempt_evaluation_exhausted",
                "attempt_evaluation_history_updated",
                "attempt_registered",
                "attempt_reacquire_requested",
                "workspace_observed",
                "invocation_checkpointed",
                "revision_operation_requested",
                "revision_operation_observed",
                "initial_sessions_ready",
                "initial_sessions_failed",
                "invocation_charge_requested",
                "attempt_setup_failed",
                "invocation_ended",
                "invocation_checkpoint_requested",
                "attempt_charge_refund_requested",
            ),
        ),
        (
            "vs_core._attempt_retirement.advance",
            (
                "retire_requested",
                "retention_required",
                "scope_reopen_requested",
                "scope_reopen_admitted",
                "reacquisition_ready",
                "scope_admission_reopened",
                "release_dependency_observed",
                "release_dependency_blocked",
            ),
        ),
        (
            "vs_core._session_turns.advance",
            (
                "registered_turn_requested",
                "turn_requested",
                "turn_observed",
                "session_observed",
                "sessions_acquire_requested",
                "invocation_charges_authorized",
                "invocation_cancellation_requested",
                "turn_inputs_reserved",
            ),
        ),
        (
            "vs_core._session_turns.advance_run_authority",
            ("run_invocation_checkpoint_requested", "run_invocation_checkpoint_observed"),
        ),
        (
            "vs_core._session_inputs.advance",
            (
                "steer_received",
                "interrupt_requested",
                "session_input_received",
                "input_reservation_requested",
                "input_acceptance_observed",
                "input_reservation_released",
                "invocation_charge_refunded",
            ),
        ),
        (
            "vs_core._measurements.advance",
            (
                "registered_job_observed",
                "registered_job_requested",
                "measurement_requested",
                "job_observed",
                "job_termination_requested",
                "jobs_drain_requested",
                "measurement_submission_observed",
            ),
        ),
        (
            "vs_core._continuations.advance",
            (
                "turn_suspended",
                "deadline_reached",
                "continuation_jobs_changed",
                "continuation_retire_requested",
                "continuation_reopen_requested",
                "continuation_scope_reopened",
            ),
        ),
        (
            "vs_core.sessions._shared_observation",
            (
                "session_drain_requested",
                "run_sessions_drain_requested",
                "invocation_checkpoint_available",
            ),
        ),
        ("vs_core.intents._observation", ("request_observed",)),
        (
            "vs_core._settlement.advance",
            (
                "assessment_submitted",
                "settlement_dependency_resolved",
                "ownership_settled",
                "attempt_settled",
            ),
        ),
        ("vs_core._adoption.advance", ("winner_proposed", "adoption_observed")),
        (
            "vs_core._intent_ledger.advance",
            (
                "request_prepared",
                "dispatch_authorized",
                "decision_dependency_resolved",
                "operation_retire_requested",
            ),
        ),
        (
            "vs_core._intent_recovery.advance",
            ("recovery_started", "recovery_ready", "reconciliation_deadline"),
        ),
        (
            "vs_core.scheduling.schedule",
            (
                "attempt_requested",
                "attempt_reopen_requested",
                "attempt_ready",
                "slot_released",
                "slot_charge_ended",
                "queue_entry_retired",
                "clock_advanced",
                "admission_control",
            ),
        ),
    )
    for tag in tags
}


AREAS = (
    (core.AttemptsEvent, core.Area.ATTEMPTS),
    (core.SessionsEvent, core.Area.SESSIONS),
    (core.EvaluationEvent, core.Area.EVALUATION),
    (core.SettlementEvent, core.Area.SETTLEMENT),
    (core.IntentsEvent, core.Area.INTENTS),
    (core.SchedulingEvent, core.Area.SCHEDULING),
)


@pytest.mark.parametrize(("event_contract", "area"), AREAS)
def test_every_event_variant_reaches_its_typed_leaf(
    event_contract: TypeAliasType,
    area: core.Area,
) -> None:
    models = variants(event_contract)
    routes = core.EVENT_ROUTES[area]
    assert Counter(routes.keys()) == Counter(models)
    for model in models:
        (tag,) = get_args(model.model_fields["kind"].annotation)
        handler = routes[cast("type[core.Signal]", model)]
        assert f"{handler.__module__}.{handler.__qualname__}" == EXPECTED_TARGET[tag]


def test_every_event_variant_has_one_dispatch_owner() -> None:
    declared = Counter(model for contract, _ in AREAS for model in variants(contract))
    routed = Counter(model for routes in core.EVENT_ROUTES.values() for model in routes)
    assert all(count == 1 for count in declared.values())
    assert routed == declared
    assert set(EXPECTED_TARGET) == {
        get_args(model.model_fields["kind"].annotation)[0] for model in declared
    }
