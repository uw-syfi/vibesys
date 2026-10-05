"""Pure shaping of a turn's terminal result and observed phase."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types.common import (
    ContractValidationError,
    InvocationRef,
    Observation,
    ObservationStatus,
    SchemaRef,
)
from .types.sessions import (
    TURN_FAILURE_DETAIL_LIMIT,
    InputAcceptanceObserved,
    InputReservationReleased,
    Invocation,
    SessionPhase,
    TurnFailureKind,
    TurnObserved,
    TurnResult,
)

if TYPE_CHECKING:
    from .types.kernel import Signal


def turn_result(
    ref: InvocationRef,
    observation: Observation,
    *,
    output_schema: SchemaRef | None = None,
    output_json: str | None = None,
    lost: bool = False,
) -> TurnResult:
    """The terminal result of a turn, with its failure kind and bounded executor text."""
    if observation.status == ObservationStatus.SUCCEEDED:
        return TurnResult(
            invocation=ref,
            observation=observation,
            output_schema=output_schema,
            output_json=output_json,
        )
    if lost:
        failure = TurnFailureKind.TRANSPORT_LOST
    elif observation.status == ObservationStatus.CANCELLED:
        failure = TurnFailureKind.CANCELLED
    else:
        failure = TurnFailureKind.PROVIDER_FAILED
    return TurnResult(
        invocation=ref,
        observation=observation,
        output_schema=output_schema,
        output_json=output_json,
        failure=failure,
        detail=observation.diagnostic[:TURN_FAILURE_DETAIL_LIMIT],
    )


def observed_phase(invocation: Invocation, event: TurnObserved) -> SessionPhase:
    observation = event.observation
    if event.output_schema is not None and event.output_schema != invocation.turn.output_schema:
        raise ContractValidationError("output_schema", "result differs from declared turn schema")
    phase = SessionPhase.EXECUTING
    if observation.status == ObservationStatus.UNKNOWN:
        phase = SessionPhase.UNKNOWN
    elif observation.terminal and observation.status != ObservationStatus.PENDING:
        phase = SessionPhase.SUSPENDED if event.suspension is not None else SessionPhase.TERMINAL
    if event.suspension is not None and (
        not observation.accepted
        or not observation.terminal
        or observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        or event.suspension.invocation != event.invocation
    ):
        raise ContractValidationError(
            "suspension", "yield requires exact accepted terminal invocation"
        )
    return phase


def is_terminal(invocation: Invocation) -> bool:
    observation = invocation.observation
    return (
        observation is not None
        and observation.terminal
        and observation.status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
    )


def acceptance_unresolved(invocation: Invocation) -> bool:
    observation = invocation.observation
    return (
        observation is not None
        and observation.terminal
        and observation.status == ObservationStatus.SUCCEEDED
        and not observation.accepted
    )


def acceptance_signals(
    invocation: Invocation, event: TurnObserved, observation: Observation
) -> tuple[Signal, ...]:
    if observation.accepted and observation.status != ObservationStatus.UNKNOWN:
        return (InputAcceptanceObserved(invocation=event.invocation, observation=observation),)
    if is_terminal(invocation) and observation.status in (
        ObservationStatus.REJECTED,
        ObservationStatus.FAILED,
        ObservationStatus.CANCELLED,
    ):
        return (InputReservationReleased(invocation=event.invocation, observation=observation),)
    return ()
