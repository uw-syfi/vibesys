"""Every closed wave-1 event reaches its frozen public area reducer."""

from collections.abc import Callable
from enum import Enum
from types import UnionType
from typing import Annotated, Literal, TypeAliasType, cast, get_args, get_origin

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


def minimal_model(model: type[BaseModel]) -> BaseModel:
    """Validate deterministic inhabitants of required fields through their contracts."""
    return model(
        **{
            name: minimal_value(field.annotation)
            for name, field in model.model_fields.items()
            if field.is_required()
        }
    )


def minimal_value(annotation: object) -> object:
    if isinstance(annotation, TypeAliasType):
        return minimal_value(annotation.__value__)
    origin = get_origin(annotation)
    if origin in (Annotated, UnionType):
        return minimal_value(get_args(annotation)[0])
    if origin is Literal:
        return get_args(annotation)[0]
    if origin is tuple:
        return ()
    if origin is frozenset:
        return frozenset()
    return minimal_scalar(annotation)


def minimal_scalar(annotation: object) -> object:
    primitives: dict[object, object] = {
        str: "value",
        int: 1,
        float: 0.0,
        bool: False,
        type(None): None,
    }
    if annotation in primitives:
        return primitives[annotation]
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return next(iter(annotation))
    assert isinstance(annotation, type)
    assert issubclass(annotation, BaseModel)
    return minimal_model(annotation)


EXPECTED_SUBAREA = {
    tag: subarea
    for subarea, tags in (
        (
            "_attempt_acquisition",
            (
                "attempt_admitted",
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
            "_attempt_retirement",
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
            "_session_turns",
            (
                "registered_turn_requested",
                "turn_requested",
                "turn_observed",
                "session_observed",
                "sessions_acquire_requested",
                "invocation_charges_authorized",
                "invocation_cancellation_requested",
                "turn_inputs_reserved",
                "session_drain_requested",
                "invocation_checkpoint_available",
            ),
        ),
        (
            "_session_inputs",
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
            "_measurements",
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
            "_continuations",
            (
                "turn_suspended",
                "deadline_reached",
                "continuation_jobs_changed",
                "continuation_retire_requested",
                "continuation_reopen_requested",
                "continuation_scope_reopened",
            ),
        ),
        ("_settlement", ("assessment_submitted", "ownership_settled", "attempt_settled")),
        ("_adoption", ("winner_proposed", "adoption_observed")),
        (
            "_intent_ledger",
            (
                "request_prepared",
                "dispatch_authorized",
                "request_observed",
                "decision_dependency_resolved",
                "operation_retire_requested",
            ),
        ),
        ("_intent_recovery", ("recovery_started", "recovery_ready", "reconciliation_deadline")),
        (
            "scheduling",
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
    (core.AttemptsEvent, core.advance_attempt, core.AttemptsContext, core.Area.ATTEMPTS),
    (core.SessionsEvent, core.advance_session, core.SessionsContext, core.Area.SESSIONS),
    (core.EvaluationEvent, core.advance_evaluation, core.EvaluationContext, core.Area.EVALUATION),
    (core.SettlementEvent, core.settle, core.SettlementContext, core.Area.SETTLEMENT),
    (core.IntentsEvent, core.advance_intent, core.IntentsContext, core.Area.INTENTS),
    (core.SchedulingEvent, core.schedule, core.SchedulingContext, core.Area.SCHEDULING),
)


@pytest.mark.parametrize(("event_contract", "reducer", "context_model", "area"), AREAS)
def test_every_event_variant_reaches_its_typed_leaf(
    event_contract: TypeAliasType,
    reducer: Callable[..., core.AreaChange],
    context_model: type[core.AreaContext],
    area: core.Area,
) -> None:
    state = core.initial_state()
    context = context_model(**{name: getattr(state, name) for name in context_model.model_fields})
    original = state.model_dump_json()
    for model in variants(event_contract):
        event = cast("core.Signal", minimal_model(model))
        with pytest.raises(core.KernelNotImplementedError) as error:
            reducer(getattr(state, area.value), context, event)
        assert error.value.area == area
        assert error.value.event_kind == event.kind
        assert error.value.subarea == EXPECTED_SUBAREA[event.kind]
        assert state.model_dump_json() == original
