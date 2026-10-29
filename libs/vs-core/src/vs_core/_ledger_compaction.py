"""Spent polls leave the ledger, so a run's durable state does not grow with its job polls.

Core polls every running evaluation job until it ends, and each poll is a request record. The
job keeps its latest observation itself, so once a poll is answered and a newer poll of the
same job exists, the poll's record is never read again: it only made each commit, which writes
the whole state, cost more with every hour of the run (FINDINGS S171).

A poll is *spent*, and leaves the ledger, when all of these hold:

- core named it for its job (``poll_family``), so core alone could name it again;
- the job has a poll of a later observation in the ledger, so ``_poll`` never names this one
  again (it names polls after the job's observation, which only grows);
- it completed with its own answer: no failure, no retry, no outcome, and an answer that is
  terminal, released and names no descendant, so no child or release proof rests on it;
- no reconciliation command addresses it, no other request targets or depends on it, no child
  lease names it as a source, no recorded wait names it, and recovery holds no unresolved
  check of it.

The newest poll of every job is kept, ended or not: it is the tombstone of the polls before
it. An answer to a spent poll (a duplicate delivery) is recognised by it (``spent_answer``)
and changes nothing, as a repeated answer to a kept record changes nothing.

Compaction is a pure function of the state, applied at the end of every step, so a replay of
the same events prunes the same records, and the request ids, sequences and events core
produces are the same with and without it (a property test drives both). Recovery's resolved
checks of spent polls go with them, and so do their ids in their decision's receipt: both
were only ever read for the requests that are kept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._measurements import poll_family
from ._waits import recorded_waits
from .types.evaluation import ObserveOwnedJob
from .types.records import BlockRequest, CancelOwnedResource, InspectRequest, RequestPhase

if TYPE_CHECKING:
    from .types.common import RequestId
    from .types.kernel import CoreState
    from .types.records import RecordsState, RequestObserved, RequestRecord


def compact_ledger(state: CoreState) -> CoreState:
    """``state`` without its spent polls (see the module); the very object when none is spent."""
    spent = _spent(state)
    if not spent:
        return state
    records = state.records
    kept = tuple(record for record in records.records if record.request_id not in spent)
    recovery = records.recovery
    checks = tuple(check for check in recovery.checks if check.target not in spent)
    receipts = tuple(
        receipt.model_copy(
            update={"request_ids": tuple(i for i in receipt.request_ids if i not in spent)}
        )
        if any(identity in spent for identity in receipt.request_ids)
        else receipt
        for receipt in state.run.receipts
    )
    return state.model_copy(
        update={
            "records": records.model_copy(
                update={
                    "records": kept,
                    "recovery": recovery
                    if len(checks) == len(recovery.checks)
                    else recovery.model_copy(update={"checks": checks}),
                }
            ),
            "run": state.run
            if all(new is old for new, old in zip(receipts, state.run.receipts, strict=True))
            else state.run.model_copy(update={"receipts": receipts}),
        }
    )


def spent_answer(state: RecordsState, event: RequestObserved) -> bool:
    """Whether ``event`` answers a poll compaction removed: one the ledger no longer holds.

    The ledger holds a newer poll of the same job, so the answered poll was named, answered
    and then pruned; a repeated answer to a record the ledger kept changes nothing either.
    """
    identity = event.observation.request_id
    family = poll_family(identity)
    if family is None or state.positions(identity):
        return False
    prefix, sequence = family
    return any(
        (newer := poll_family(record.request_id)) is not None
        and newer[0] == prefix
        and newer[1] > sequence
        for record in state.records
        if isinstance(record.request, ObserveOwnedJob)
    )


def _spent(state: CoreState) -> frozenset[RequestId]:
    newest: dict[str, int] = {}
    finished: list[tuple[RequestId, str, int]] = []
    for record in state.records.records:
        if not isinstance(record.request, ObserveOwnedJob):
            continue
        family = poll_family(record.request_id)
        if family is None:
            continue
        prefix, sequence = family
        newest[prefix] = max(newest.get(prefix, sequence), sequence)
        if _answered(record):
            finished.append((record.request_id, prefix, sequence))
    superseded = [identity for identity, prefix, sequence in finished if sequence < newest[prefix]]
    if not superseded:
        return frozenset()
    pinned = _pinned(state)
    return frozenset(identity for identity in superseded if identity not in pinned)


def _answered(record: RequestRecord) -> bool:
    """A poll that ended with its own answer, on which no proof of ownership or release rests."""
    observation = record.observation
    return (
        record.phase == RequestPhase.COMPLETED
        and record.failure is None
        and record.retry_at is None
        and record.suspension is None
        and record.evaluation_result is None
        and record.outcome_json is None
        and not record.addressed_by
        and observation is not None
        and observation.terminal
        and observation.released
        and observation.children_complete
        and not observation.descendants
    )


def _pinned(state: CoreState) -> frozenset[RequestId]:
    """Every request id some part of the state still names besides its own record."""
    records = state.records
    named: set[RequestId] = set()
    for record in records.records:
        request = record.request
        named.update(request.depends_on)
        if isinstance(request, InspectRequest | CancelOwnedResource | BlockRequest):
            named.add(request.target)
    for child in records.children:
        named.update(child.source_requests)
        named.update(mark.source_request for mark in child.observation_watermarks)
    for check in records.recovery.checks:
        if check.resolution != "terminal":
            named.add(check.target)
        if check.inspection is not None:
            named.add(check.inspection)
    for _subject, wait in recorded_waits(state):
        named.update(wait.requests)
    return frozenset(named)
