"""A turn's limit never counts the time the run's capacity gate held it for its provider.

An agent turn runs inside one executor call, and so does the run's capacity gate: when the
provider stops a turn (an outage, a spent quota) the gate waits inside that call before it
sends the turn again. That time is the provider's, as a job's queue time is the cluster's
(``_wait_clock``), so it is never charged to the turn's limit. Before (S179), a two hour
outage, inside the gate's six hour outage budget, ended every planner turn at its 30 minute
limit and the run with them.

The gate reports each turn's hold as a cumulative ``CapacityHold`` (the host's control
bridge makes it a ``TurnHeld``), and the turn's open dispatch record keeps the latest
(``held``). Every stated limit stays as it was stated; the record's bound is derived from it
in ``_wait_clock.turn_bound``, which ``record_bound`` uses for every record:

- **Credit.** A hold that ended credits its length; the gate bounds each one. While the gate
  holds the turn for a backoff, the turn may stay held up to the budget, so its bound moves
  by the budget; once released, by the time it was held, never more than the budget.
- **Resend.** The send after a release has at least the gate's ``resend_grace`` (about one
  failing send) whatever own time the turn had left, so an outage that started late in the
  turn does not cut it while its resend is still failing; the next stop reopens the hold.
- **Parked.** A hold that waits for the operator or a timed resume (the run is paused for
  capacity) has no limit of its own: the turn ends with the run. The gate ends a parked hold
  when the run resumes, so the time the operator took is credited whole.
- **Late.** A hold that began after the turn's own time ran out cannot revive its limit.
- **Ceiling.** As every moved limit, a bound carried past the run deadline ends with the run.

A turn core ends while the gate holds it is lost to its provider, not by its transport
(``held_when_lost``): its result says ``PROVIDER_UNAVAILABLE``, and the agents library asks
it again without counting it against the role's lost turns, nor against the chain of
re-asks core bounds (``lost_to_provider``): the gate's budget and its pause bound the outage.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ._registry import ContractError
from ._request_forward import turn_of
from .types.common import ExecuteRegisteredOperation, ObservationStatus
from .types.kernel import AreaChange
from .types.records import RequestPhase, TurnHeld
from .types.sessions import DispatchTurn, ResumeSessionTurn, TurnFailureKind

if TYPE_CHECKING:
    from .types.kernel import RecordsContext, SessionsContext
    from .types.records import CapacityHold, RecordsEvent, RecordsState, RequestRecord
    from .types.sessions import Invocation

_HELD = (RequestPhase.DISPATCHED, RequestPhase.RECONCILING)
"""The phases of a dispatch record whose turn the gate can be holding."""
_ENDED = (RequestPhase.BLOCKED, RequestPhase.COMPLETED)
_NOT_RETURNED = (ObservationStatus.PENDING, ObservationStatus.CANCELLED)


def held(
    state: RecordsState, context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Keep the gate's latest report on the turn's open dispatch record; else change nothing."""
    if not isinstance(event, TurnHeld):
        raise ContractError(("event",), "event is not a capacity hold report")

    def reported(record: RequestRecord) -> RequestRecord:
        if record.phase not in _HELD or record.capacity_hold == event.hold:
            return record
        turn = turn_of(context, record)
        if turn is None or turn.invocation_id != event.invocation_id:
            return record
        return record.model_copy(update={"capacity_hold": event.hold})

    records = tuple(reported(record) for record in state.records)
    if records == state.records:
        return AreaChange(state=state)
    return AreaChange(state=state.model_copy(update={"records": records}))


def held_limit(stated: float, hold: CapacityHold) -> float:
    """When a turn with stated limit ``stated`` ends, given the gate's latest ``hold`` of it.

    Before the run deadline caps it (``_wait_clock.turn_bound``); ``inf`` while it is parked.
    """
    at = stated + hold.credit
    if hold.since is not None and hold.since > at:
        return at  # late: the hold began after the turn's own time ran out
    if hold.since is not None:
        if hold.parked:
            return math.inf
        current = hold.budget if hold.released_at is None else hold.released_at - hold.since
        at += min(current, hold.budget)
    if hold.released_at is not None:
        at = max(at, hold.released_at + hold.resend_grace)
    return at


def lost_without_debit(records: RecordsState, invocation: Invocation) -> bool:
    """Whether ``invocation`` ended ``PROVIDER_UNAVAILABLE``: core ended it while the gate held it.

    As ``turn_result`` decides it: the turn was cancelled (core released it lost) while held.
    A re-ask after such a loss is the provider's, so it is no link of the chain of corrections
    and re-asks core bounds (``MAX_REASK_CHAIN``).
    """
    observation = invocation.observation
    return (
        observation is not None
        and observation.status is ObservationStatus.CANCELLED
        and (held_when_lost(records, invocation) or interrupted_by_epoch(records, invocation))
    )


def interrupted_by_epoch(records: RecordsState, invocation: Invocation) -> bool:
    """The unfinished dispatch belongs to an earlier host, including a crashed epoch."""
    return any(
        _dispatches(record, invocation)
        and record.dispatched_in is not None
        and record.dispatched_in < records.recovery.epoch
        and (record.observation is None or record.observation.status in _NOT_RETURNED)
        for record in records.records
    )


def held_when_lost(records: RecordsState, invocation: Invocation) -> bool:
    """Whether core ended ``invocation``'s dispatch while the capacity gate held the turn.

    The record ended (core blocks it at its bound or by a watch, and the block completes it),
    and an ended record takes no later report, so its hold is the gate's last word before the
    end. That report says the gate was holding the turn, and the executor never reported the
    call's end: the record's observation is none, the turn's acknowledgement, or the cancel
    core made. A call that returned (say with the error of a turn the gate gave up) is not
    held any more, whatever report of it is still on its way.
    """
    return any(
        record.phase in _ENDED
        and record.capacity_hold is not None
        and record.capacity_hold.holding
        and _within_its_time(record, record.capacity_hold)
        and (record.observation is None or record.observation.status in _NOT_RETURNED)
        and _dispatches(record, invocation)
        for record in records.records
    )


def _within_its_time(record: RequestRecord, hold: CapacityHold) -> bool:
    """Whether the hold began before the turn's own time ran out (else it is late, as a bound)."""
    return hold.since is not None and hold.since <= record.until.at + hold.credit


def _dispatches(record: RequestRecord, invocation: Invocation) -> bool:
    request = record.request
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return request.turn == invocation.turn and request.scope == invocation.scope
    return (
        isinstance(request, ExecuteRegisteredOperation)
        and invocation.registered_operation is not None
        and request.operation_id == invocation.registered_operation
        and request.scope == invocation.scope
    )


def lost_failure(context: SessionsContext, invocation: Invocation) -> TurnFailureKind:
    """A lost physical turn is unfinished when its epoch or capacity hold interrupted it."""
    if context.run.park is not None or interrupted_by_epoch(context.records, invocation):
        return TurnFailureKind.EPOCH_INTERRUPTED
    if held_when_lost(context.records, invocation):
        return TurnFailureKind.PROVIDER_UNAVAILABLE
    return TurnFailureKind.TRANSPORT_LOST
