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
from vs_proto.api import (
    RunEnded as RunEndedRecord,
)

from ._refusal import RecordRefusedError, RefusalReason
from .types.common import Value
from .types.kernel import CoreEvent
from .types.records import Request, RequestObserved
from .types.settlement import RunResultProposal
from .types.steps import (
    Cancelled,
    CancelReason,
    Failed,
    Finished,
    Interruption,
    OwnerEvent,
    StepResult,
)

if TYPE_CHECKING:
    from ._actions import End, Run
    from ._registry import OperationRegistry
    from .types.common import RequestId

SCHEMA_VERSION = 1
"""The version of the canonical encoding of every body in the core records."""


class _FinishedBody(Value):
    """What a ``StepFinished`` holds of a finished step: its report and the owner events."""

    report: RequestObserved
    owner_events: tuple[OwnerEvent, ...] = ()


_EVENT = TypeAdapter(CoreEvent)
_REQUEST = TypeAdapter(Request)
_FINISHED = TypeAdapter(_FinishedBody)
_RESULT = TypeAdapter(RunResultProposal)

_CANCEL_REASONS = {
    CancelReason.DEADLINE: StepCancelReason.DEADLINE,
    CancelReason.PAUSE_NOW: StepCancelReason.PAUSE_NOW,
    CancelReason.DRAIN_EXPIRED: StepCancelReason.DRAIN_EXPIRED,
    CancelReason.RUN_ENDING: StepCancelReason.RUN_ENDING,
    CancelReason.WITHDRAWN: StepCancelReason.WITHDRAWN,
    CancelReason.INTERRUPTED: StepCancelReason.INTERRUPTED,
}
_CANCEL_NAMES = {stored: reason for reason, stored in _CANCEL_REASONS.items()}
_INTERRUPTIONS = {
    Interruption.PAUSE: InterruptionReason.PAUSE,
    Interruption.SHUTDOWN: InterruptionReason.SHUTDOWN,
    Interruption.RAISED: InterruptionReason.RAISED,
}
_INTERRUPTION_NAMES = {stored: why for why, stored in _INTERRUPTIONS.items()}


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
            finished = _FinishedBody(report=result.report, owner_events=result.owner_events)
            body = Oneof("finished", _FINISHED.dump_json(finished))
        case Failed():
            body = Oneof("failed", result.model_dump_json().encode())
        case Cancelled():
            body = Oneof("cancelled", _CANCEL_REASONS[result.reason])
        case _:
            assert_never(result)
    return StepFinished(tag=tag.root, attempt=attempt, schema_version=SCHEMA_VERSION, result=body)


def interrupted_message(tag: RequestId, attempt: int, why: Interruption) -> AttemptInterrupted:
    """An attempt that ended without a result: its step stays open."""
    return AttemptInterrupted(tag=tag.root, attempt=attempt, reason=_INTERRUPTIONS[why])


def decode_interruption(message: AttemptInterrupted) -> Interruption:
    """Why the attempt of an ``AttemptInterrupted`` ended, refused when this version has no such reason."""
    return _known(_INTERRUPTION_NAMES, message.reason, "an interruption")


def _known[S, T](names: dict[S, T], stored: S, what: str) -> T:
    """The reason ``stored`` names, or a refusal of a log that holds one this version does not know."""
    try:
        return names[stored]
    except KeyError:
        message = f"the log holds {what} ({stored!r}) this version does not know"
        raise RecordRefusedError(RefusalReason.UNREADABLE, message) from None


def ended_message(end: End) -> RunEndedRecord:
    """The end of the run: its result, published once nothing is open."""
    return RunEndedRecord(schema_version=SCHEMA_VERSION, result=_RESULT.dump_json(end.result))


def decode_run_ended(message: RunEndedRecord) -> RunResultProposal:
    """The result a ``RunEnded`` record published, refused when written by another encoding."""
    _same_encoding(message.schema_version)
    return _RESULT.validate_json(message.result)


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
        case Oneof(field="finished", value=stored):
            body = _FINISHED.validate_json(stored, context=_context(registry))
            return Finished(
                observation=body.report.observation,
                observed=body.report,
                owner_events=body.owner_events,
            )
        case Oneof(field="failed", value=failure):
            return Failed.model_validate_json(failure)
        case Oneof(field="cancelled", value=reason):
            return Cancelled(reason=_known(_CANCEL_NAMES, reason, "a cancel reason"))
        case _:
            message_ = "a step result names finished, failed or cancelled"
            raise RecordRefusedError(RefusalReason.UNREADABLE, message_)


def _same_encoding(version: int) -> None:
    if version != SCHEMA_VERSION:
        message = f"core record encoding version {version} is not {SCHEMA_VERSION}"
        raise RecordRefusedError(RefusalReason.UNREADABLE, message)


def _context(registry: OperationRegistry | None) -> dict[str, object]:
    return {} if registry is None else {"operation_registry": registry}
