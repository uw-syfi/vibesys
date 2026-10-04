"""Bounded immutable operation pages and deltas, isolated by host capability.

The service supplies complete trusted observations. This module owns presentation
and retains at most 128 snapshots; evicted cursors fail explicitly. Detail reads
are explicit and unbounded. No clocks, agents or executor calls enter this module.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass
from hashlib import sha256

from vs_evaluation.agent_evidence import EvidenceOutcome
from vs_evaluation.agent_models import (
    MAX_TOOL_PAGE_CHARS,
    EvaluationOperationObservation,
    RunOperationsCall,
    RunOperationsReply,
)
from vs_evaluation.models import EvaluationState
from vs_evaluation.profiler_models import ProfilerOperationState, ProfilerRunObservation

_MAX_SNAPSHOTS = 128
_ACTIVE = {
    EvaluationState.QUEUED,
    EvaluationState.STARTING,
    EvaluationState.RUNNING,
    ProfilerOperationState.QUEUED,
    ProfilerOperationState.RUNNING,
}
_Row = EvaluationOperationObservation | ProfilerRunObservation


class OperationCursorError(ValueError):
    """An operation cursor is invalid, expired or belongs to another grant."""

    def __init__(self, field: str) -> None:
        """Name the rejected cursor or detail field."""
        super().__init__(f"{field}: invalid, expired or unauthorized operation reference")


@dataclass(frozen=True)
class _Snapshot:
    token: str
    complete: tuple[_Row, ...]
    selected: tuple[_Row, ...]


def _identity(row: _Row) -> str:
    kind, identity = (
        ("evaluation", row.handle_id)
        if isinstance(row, EvaluationOperationObservation)
        else ("profiler", row.operation_id)
    )
    return sha256(f"{kind}:{identity}".encode()).hexdigest()


def _brief(row: _Row) -> _Row:
    if isinstance(row, EvaluationOperationObservation):
        return row.model_copy(
            update={
                "evidence_ids": (),
                "stage_outcomes": tuple(
                    stage.model_copy(update={"summary_tail": None}) for stage in row.stage_outcomes
                ),
            }
        )
    return row.model_copy(
        update={
            "request": row.request[:128],
            "work": row.work.model_copy(update={"focus": row.work.focus[:64]}),
            "trusted_evidence_ids": row.trusted_evidence_ids[:1],
        }
    )


def _reply(
    rows: tuple[_Row, ...],
    *,
    cursor: str | None,
    snapshot: str,
    omitted: tuple[_Row, ...],
    detail: tuple[str, ...] = (),
) -> RunOperationsReply:
    return RunOperationsReply(
        evaluations=tuple(row for row in rows if isinstance(row, EvaluationOperationObservation)),
        profiler_operations=tuple(row for row in rows if isinstance(row, ProfilerRunObservation)),
        next_cursor=cursor,
        snapshot_cursor=snapshot,
        omitted_by_state=dict(Counter(row.state for row in omitted)),
        detail_required=detail,
        references=tuple(
            _identity(row) for row in rows if isinstance(row, EvaluationOperationObservation)
        )
        + tuple(_identity(row) for row in rows if isinstance(row, ProfilerRunObservation)),
        omitted_failed_verdicts=sum(
            isinstance(row, EvaluationOperationObservation)
            and any(stage.outcome is EvidenceOutcome.FAILED for stage in row.stage_outcomes)
            for row in omitted
        ),
    )


class OperationPages:
    """Read stable bounded pages with complete omission counts and exact deltas."""

    def __init__(self) -> None:
        """Retain a bounded sequence of immutable snapshots."""
        self._snapshots: OrderedDict[str, _Snapshot] = OrderedDict()
        self._sequence = 0

    def _get(self, reference: str, token: str, field: str) -> _Snapshot:
        snapshot = self._snapshots.get(reference)
        if snapshot is None or snapshot.token != token:
            raise OperationCursorError(field)
        return snapshot

    def read(self, call: RunOperationsCall, source: RunOperationsReply) -> RunOperationsReply:
        """Return a 4000-character page, or explicitly requested complete detail."""
        complete = (*source.evaluations, *source.profiler_operations)
        if call.reference_id is not None:
            rows = tuple(row for row in complete if _identity(row) == call.reference_id)
            if not rows:
                raise OperationCursorError("reference_id")
            return RunOperationsReply(
                evaluations=tuple(
                    row for row in rows if isinstance(row, EvaluationOperationObservation)
                ),
                profiler_operations=tuple(
                    row for row in rows if isinstance(row, ProfilerRunObservation)
                ),
            )
        if call.cursor is not None:
            reference, separator, position = call.cursor.partition(":")
            snapshot = self._get(reference, call.token, "cursor")
            if not separator or not position.isdecimal() or int(position) >= len(snapshot.selected):
                raise OperationCursorError("cursor")
            return self._page(reference, snapshot, int(position))
        previous = (
            self._get(call.since, call.token, "since")
            if call.since
            else next(
                (item for item in reversed(self._snapshots.values()) if item.token == call.token),
                None,
            )
        )
        old = {_identity(row): row for row in previous.complete} if previous else {}
        changed = {_identity(row) for row in complete if old.get(_identity(row)) != row}
        selected = tuple(row for row in complete if not call.since or _identity(row) in changed)
        selected = tuple(
            sorted(
                selected,
                key=lambda row: (
                    row.state not in _ACTIVE,
                    _identity(row) not in changed,
                    -(
                        row.submission_index
                        if isinstance(row, EvaluationOperationObservation)
                        else complete.index(row)
                    ),
                    _identity(row),
                ),
            )
        )
        self._sequence += 1
        reference = sha256(f"{call.token}:{self._sequence}".encode()).hexdigest()
        snapshot = _Snapshot(call.token, complete, selected)
        self._snapshots[reference] = snapshot
        if len(self._snapshots) > _MAX_SNAPSHOTS:
            self._snapshots.popitem(last=False)
        return self._page(reference, snapshot, 0)

    def _page(self, reference: str, snapshot: _Snapshot, start: int) -> RunOperationsReply:
        rows: tuple[_Row, ...] = ()
        detail: tuple[str, ...] = ()
        position = start
        while position < len(snapshot.selected):
            row = snapshot.selected[position]
            proposed = (*rows, _brief(row))
            end = position + 1
            cursor = f"{reference}:{end}" if end < len(snapshot.selected) else None
            reply = _reply(
                proposed,
                cursor=cursor,
                snapshot=reference,
                omitted=snapshot.selected[:start] + snapshot.selected[end:],
                detail=detail,
            )
            if len(reply.model_dump_json()) > MAX_TOOL_PAGE_CHARS:
                if rows or detail:
                    break
                detail = (_identity(row),)
            else:
                rows = proposed
            position = end
        cursor = f"{reference}:{position}" if position < len(snapshot.selected) else None
        omitted = snapshot.selected[:start] + snapshot.selected[position:]
        if detail:
            omitted += (snapshot.selected[start],)
        reply = _reply(rows, cursor=cursor, snapshot=reference, omitted=omitted, detail=detail)
        if len(reply.model_dump_json()) > MAX_TOOL_PAGE_CHARS:
            raise OperationCursorError("reference_id")
        return reply


__all__ = ["OperationCursorError", "OperationPages"]
