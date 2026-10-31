"""The records core folds from a run.log: their canonical encoding and the messages that carry it.

Events, requests, observations and failures are the canonical JSON of today's validated values,
versioned by ``SCHEMA_VERSION``, until those values are proto messages (run-recovery.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from protobuf import Oneof
from pydantic import TypeAdapter

from vs_proto.api import (
    AttemptInterrupted,
    CoreInput,
    InterruptionReason,
    StepCancelReason,
    StepFinished,
    StepStarted,
)

from .types.kernel import CoreEvent
from .types.records import Request, RequestObserved
from .types.steps import Cancelled, CancelReason, Failed, Finished, StepResult

if TYPE_CHECKING:
    from ._actions import Run
    from ._registry import OperationRegistry
    from .types.common import RequestId

SCHEMA_VERSION = 1
"""The version of the canonical encoding of every body in the core records."""

_EVENT = TypeAdapter(CoreEvent)
_REQUEST = TypeAdapter(Request)
_OBSERVED = TypeAdapter(RequestObserved)

_CANCEL_REASONS = {
    CancelReason.DEADLINE: StepCancelReason.DEADLINE,
    CancelReason.PAUSE_NOW: StepCancelReason.PAUSE_NOW,
    CancelReason.DRAIN_EXPIRED: StepCancelReason.DRAIN_EXPIRED,
    CancelReason.RUN_ENDING: StepCancelReason.RUN_ENDING,
    CancelReason.WITHDRAWN: StepCancelReason.WITHDRAWN,
    CancelReason.INTERRUPTED: StepCancelReason.INTERRUPTED,
}
_CANCEL_NAMES = {stored: reason for reason, stored in _CANCEL_REASONS.items()}


def input_message(event: CoreEvent) -> CoreInput:
    """The record body of one external input core consumed."""
    return CoreInput(schema_version=SCHEMA_VERSION, event=_EVENT.dump_json(event))


def started_message(run: Run) -> StepStarted:
    """The durable intent to run the attempt ``run`` asks for, appended before any I/O."""
    return StepStarted(
        tag=run.tag.root,
        attempt=run.attempt,
        schema_version=SCHEMA_VERSION,
        request=_REQUEST.dump_json(run.step),
        deadline_at=run.deadline_at,
    )


def finished_message(tag: RequestId, attempt: int, result: StepResult) -> StepFinished:
    """The one result of a step: the attempt that produced it, or its last when core settled it."""
    match result:
        case Finished():
            body = Oneof("finished", result.report.model_dump_json().encode())
        case Failed():
            body = Oneof("failed", result.model_dump_json().encode())
        case Cancelled():
            body = Oneof("cancelled", _CANCEL_REASONS[result.reason])
        case _:
            assert_never(result)
    return StepFinished(tag=tag.root, attempt=attempt, schema_version=SCHEMA_VERSION, result=body)


def interrupted_message(
    tag: RequestId, attempt: int, reason: InterruptionReason
) -> AttemptInterrupted:
    """An attempt that ended without a result: its step stays open."""
    return AttemptInterrupted(tag=tag.root, attempt=attempt, reason=reason)


def decode_event(message: CoreInput, registry: OperationRegistry | None) -> CoreEvent:
    """The event of a ``CoreInput``, refused when written by another encoding."""
    _same_encoding(message.schema_version)
    return _EVENT.validate_json(message.event, context=_context(registry))


def decode_request(message: StepStarted, registry: OperationRegistry | None) -> Request:
    """The request of a ``StepStarted``, refused when written by another encoding."""
    _same_encoding(message.schema_version)
    return _REQUEST.validate_json(message.request, context=_context(registry))


def decode_result(message: StepFinished, registry: OperationRegistry | None) -> StepResult:
    """The result of a ``StepFinished``, refused when written by another encoding."""
    _same_encoding(message.schema_version)
    match message.result:
        case Oneof(field="finished", value=report):
            observed = _OBSERVED.validate_json(report, context=_context(registry))
            return Finished(observation=observed.observation, observed=observed)
        case Oneof(field="failed", value=failure):
            return Failed.model_validate_json(failure)
        case Oneof(field="cancelled", value=reason):
            return Cancelled(reason=_CANCEL_NAMES[reason])
        case _:
            message_ = "a step result names finished, failed or cancelled"
            raise ValueError(message_)


def _same_encoding(version: int) -> None:
    if version != SCHEMA_VERSION:
        message = f"core record encoding version {version} is not {SCHEMA_VERSION}"
        raise ValueError(message)


def _context(registry: OperationRegistry | None) -> dict[str, object]:
    return {} if registry is None else {"operation_registry": registry}
