"""Bounded evidence projections and capability-scoped snapshot continuations.

The service owns one pager. Snapshots survive changes to the backend listing,
but not a service restart. Unknown and expired cursors fail explicitly.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from vs_evaluation.agent_models import (
    MAX_TOOL_PAGE_CHARS,
    EvidenceArgs,
    EvidenceCoverage,
    EvidenceOverviewRow,
    EvidenceReply,
)

if TYPE_CHECKING:
    from vs_evaluation.agent_evidence import TrustedEvidence

MAX_EVIDENCE_PAGE_CHARS = MAX_TOOL_PAGE_CHARS
_MAX_SNAPSHOTS = 128
_MAX_ROW_CHARS = 1000


class EvidencePageFailure(StrEnum):
    """Explicitly rejected evidence references and continuations."""

    UNKNOWN_CURSOR = "unknown or expired evidence cursor for this capability"
    UNKNOWN_REFERENCE = "unknown evidence reference_id in this selection"


class EvidencePageError(ValueError):
    """An evidence query could not resolve its authorized reference."""

    def __init__(self, failure: EvidencePageFailure) -> None:
        """Name the rejected reference or continuation."""
        super().__init__(failure.value)


@dataclass(frozen=True)
class _Snapshot:
    evidence: tuple[TrustedEvidence, ...]
    workload: str | None
    offset: int


def _workload(
    record: TrustedEvidence, workload: str | None
) -> Literal["unknown", "matched", "mismatched"]:
    if workload is None:
        return "unknown"
    return "matched" if record.fingerprints.workload.value == workload else "mismatched"


def _coverage(records: tuple[TrustedEvidence, ...], workload: str | None) -> EvidenceCoverage:
    identities = {_workload(record, workload) for record in records}
    mismatch = next(iter(identities)) if len(identities) == 1 else "mixed"
    if not identities:
        mismatch = "unknown"
    return EvidenceCoverage(
        by_kind=dict(Counter(record.kind for record in records)),
        by_outcome=dict(Counter(record.outcome for record in records)),
        workload_mismatch=mismatch,
    )


def _overview(record: TrustedEvidence, workload: str | None) -> EvidenceOverviewRow:
    row = EvidenceOverviewRow(
        evidence_id=record.evidence_id,
        kind=record.kind,
        outcome=record.outcome,
        workload_mismatch=_workload(record, workload),
        metrics_omitted=len(record.metrics),
        partial_measurement_available=record.partial_measurement is not None,
    )
    for metric in record.metrics:
        proposed = row.model_copy(
            update={"metrics": (*row.metrics, metric), "metrics_omitted": row.metrics_omitted - 1}
        )
        if len(proposed.model_dump_json()) <= _MAX_ROW_CHARS:
            row = proposed
    return row


class EvidencePages:
    """Project trusted records without changing their trust or advisory semantics."""

    def __init__(self) -> None:
        """Retain a bounded set of immutable authorized continuations."""
        self._snapshots: dict[tuple[str, str], _Snapshot] = {}

    def query(
        self, records: tuple[TrustedEvidence, ...], args: EvidenceArgs, *, capability: str
    ) -> EvidenceReply:
        """Return a bounded stable page, or explicit complete detail.

        Cursors bind the original evidence-kind and workload selection. A
        continuation ignores new backend records, so each reference appears once.
        """
        if args.cursor is not None:
            snapshot = self._snapshots.get((capability, args.cursor))
            if snapshot is None:
                raise EvidencePageError(EvidencePageFailure.UNKNOWN_CURSOR)
        else:
            snapshot = _Snapshot(
                tuple(sorted(records, key=lambda item: (-item.accepted_round, item.evidence_id))),
                args.workload,
                0,
            )
        coverage = _coverage(snapshot.evidence, snapshot.workload)
        if args.reference_id is not None:
            record = next(
                (record for record in snapshot.evidence if record.evidence_id == args.reference_id),
                None,
            )
            if record is None:
                raise EvidencePageError(EvidencePageFailure.UNKNOWN_REFERENCE)
            return EvidenceReply(evidence=(record,), coverage=coverage)
        if args.full:
            return EvidenceReply(evidence=snapshot.evidence, coverage=coverage)
        rows: list[EvidenceOverviewRow] = []
        offset = snapshot.offset
        while offset < len(snapshot.evidence):
            proposed = [*rows, _overview(snapshot.evidence[offset], snapshot.workload)]
            reply = EvidenceReply(
                evidence=tuple(proposed),
                coverage=coverage,
                omitted=len(snapshot.evidence) - offset - 1,
                next_cursor="0" * 64,
            )
            if len(reply.model_dump_json()) > MAX_EVIDENCE_PAGE_CHARS:
                break
            rows = proposed
            offset += 1
        cursor = None
        if offset < len(snapshot.evidence):
            cursor = self._remember(
                capability, _Snapshot(snapshot.evidence, snapshot.workload, offset)
            )
        return EvidenceReply(
            evidence=tuple(rows),
            coverage=coverage,
            omitted=len(snapshot.evidence) - offset,
            next_cursor=cursor,
        )

    def _remember(self, capability: str, snapshot: _Snapshot) -> str:
        document = EvidenceReply(evidence=snapshot.evidence).model_dump_json()
        cursor = hashlib.sha256(
            (document + str(snapshot.offset) + str(snapshot.workload)).encode()
        ).hexdigest()
        self._snapshots[(capability, cursor)] = snapshot
        if len(self._snapshots) > _MAX_SNAPSHOTS:
            del self._snapshots[next(iter(self._snapshots))]
        return cursor


__all__ = ["MAX_EVIDENCE_PAGE_CHARS", "EvidencePageError", "EvidencePages"]
