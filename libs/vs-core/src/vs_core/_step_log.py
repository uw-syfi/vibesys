"""The records core folds from a run.log: their canonical encoding and the messages that carry it.

Events, requests, observations and failures are the canonical JSON of today's validated values,
versioned by ``SCHEMA_VERSION``, until those values are proto messages (run-recovery.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from protobuf import Oneof
from pydantic import TypeAdapter

from vs_proto.api import (
    AttemptInterrupted,
    CoreInput,
    InterruptionReason,
    RunStarted,
    StepCancelReason,
    StepFinished,
    StepStarted,
    StrategyAdvanced,
)
from vs_proto.api import (
    RunEnded as RunEndedRecord,
)

from ._refusal import RecordRefusedError, RefusalReason
from .types.common import Value
from .types.kernel import CoreEvent, CoreState
from .types.records import Request, RequestObserved
from .types.search import Discarded, Event
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
from .types.strategy import Decision

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


class _FoldBody(Value):
    """A cached fold: core's state, and the events the search has yet to hear."""

    schema_version: int = SCHEMA_VERSION
    state: CoreState
    events: tuple[Event, ...] = ()
    discarded: tuple[Discarded, ...] = ()


_EVENT = TypeAdapter(CoreEvent)
_STATE = TypeAdapter(CoreState)
_DECISIONS = TypeAdapter(tuple[Decision, ...])
_REQUEST = TypeAdapter(Request)
_FINISHED = TypeAdapter(_FinishedBody)
_RESULT = TypeAdapter(RunResultProposal)
_FOLD = TypeAdapter(_FoldBody)

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


def seed_message(state: CoreState) -> RunStarted:
    """The record body that starts a run's log: the state its fold begins from."""
    return RunStarted(schema_version=SCHEMA_VERSION, state=_STATE.dump_json(state))


def decode_seed(message: RunStarted, registry: OperationRegistry | None) -> CoreState:
    """The state a ``RunStarted`` begins the fold from, refused when written by another encoding."""
    _same_encoding(message.schema_version)
    return _STATE.validate_json(message.state, context=_context(registry))


@dataclass(frozen=True)
class SavedRun:
    """A run an older version saved, as it enters the log: what its fold starts from.

    ``inputs`` are the owner events the saved run had received and not yet applied. ``strategy``
    is the search's whole state in the driver's own encoding, ``wakes`` and ``held`` what it had
    asked for and could not hand core yet.
    """

    state: CoreState
    envelope_version: int
    strategy: bytes
    inputs: tuple[CoreEvent, ...] = ()
    wakes: tuple[float, ...] = ()
    held: tuple[Decision, ...] = ()


@dataclass(frozen=True)
class EncodedSavedRun:
    """A saved run as the canonical JSON of its values, the form a snapshot record packs.

    Packing it into a record and compressing it are the log writer's and reader's (a saved state is
    megabytes of JSON); this is the part that is a function of core's own values.
    """

    envelope_version: int
    state: bytes
    strategy: bytes
    inputs: tuple[bytes, ...] = ()
    wakes: tuple[float, ...] = ()
    held: bytes = b""
    schema_version: int = SCHEMA_VERSION


def encode_saved(saved: SavedRun) -> EncodedSavedRun:
    """The canonical JSON of the values of ``saved``."""
    return EncodedSavedRun(
        envelope_version=saved.envelope_version,
        state=_STATE.dump_json(saved.state),
        strategy=saved.strategy,
        inputs=tuple(_EVENT.dump_json(event) for event in saved.inputs),
        wakes=saved.wakes,
        held=_DECISIONS.dump_json(saved.held),
    )


def decode_saved(encoded: EncodedSavedRun, registry: OperationRegistry | None) -> SavedRun:
    """The run ``encode_saved`` wrote, refused when written by another encoding."""
    _same_encoding(encoded.schema_version)
    context = _context(registry)
    return SavedRun(
        state=_STATE.validate_json(encoded.state, context=context),
        envelope_version=encoded.envelope_version,
        strategy=encoded.strategy,
        inputs=tuple(_EVENT.validate_json(event, context=context) for event in encoded.inputs),
        wakes=encoded.wakes,
        held=_DECISIONS.validate_json(encoded.held, context=context) if encoded.held else (),
    )


def encode_fold(
    state: CoreState, events: tuple[Event, ...], discarded: tuple[Discarded, ...]
) -> bytes:
    """The canonical JSON of a fold: a state and what the search still has to hear."""
    return _FOLD.dump_json(_FoldBody(state=state, events=events, discarded=discarded))


def decode_fold(
    data: bytes, registry: OperationRegistry | None
) -> tuple[CoreState, tuple[Event, ...], tuple[Discarded, ...]]:
    """A fold written by ``encode_fold``, refused when another encoding or damaged wrote it."""
    body = _FOLD.validate_json(data, context=_context(registry))
    _same_encoding(body.schema_version)
    return body.state, body.events, body.discarded


def encode_event(event: CoreEvent) -> bytes:
    """The canonical JSON of one core event."""
    return _EVENT.dump_json(event)


def encode_held(decisions: tuple[Decision, ...]) -> bytes:
    """The canonical JSON of the decisions a search holds."""
    return _DECISIONS.dump_json(decisions)


def decode_held(
    message: StrategyAdvanced, registry: OperationRegistry | None
) -> tuple[Decision, ...]:
    """The decisions an advance record says the search holds."""
    _same_encoding(message.schema_version)
    if not message.held:
        return ()
    return _DECISIONS.validate_json(message.held, context=_context(registry))


def decode_proposals(
    message: StrategyAdvanced, registry: OperationRegistry | None
) -> tuple[CoreEvent, ...]:
    """The proposals core took in an advance, in the order it took them."""
    _same_encoding(message.schema_version)
    return tuple(
        _EVENT.validate_json(proposal, context=_context(registry)) for proposal in message.proposals
    )


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
