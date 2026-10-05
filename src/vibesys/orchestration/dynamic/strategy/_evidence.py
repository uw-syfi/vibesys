"""Which evidence and readings the strategy trusts.

Core's evidence ledger is the authority. The strategy keeps only `EvidenceKey`s,
embeds the ledger's own `EvidenceRef` values in an `InterpretEvidence` request,
and accepts back only readings for exactly the records it asked about. A reading
or a measurement result that names another request, candidate, scope, generation
or purpose, or an untrusted record, is dropped or refused with a typed reason; it
never reaches a decision.
"""

from vibesys.orchestration.dynamic.strategy._operations import EvidenceReadings
from vibesys.orchestration.dynamic.strategy._rows import AcceptedReading
from vs_core.api import (
    AttemptId,
    EvidenceKey,
    EvidenceRef,
    InvocationRef,
    MeasurementResult,
    ObservationStatus,
    RevisionRef,
    RunView,
    Scope,
)

type Purpose = str


def trusted_keys(
    event: MeasurementResult, *, scope: Scope, candidate: RevisionRef, purpose: Purpose
) -> tuple[EvidenceKey, ...]:
    """Keys of the result's evidence that is trusted and belongs to this measurement.

    The record must carry the measured scope (owner and generation), the measured
    candidate and the requested purpose, and, when the result names its submission
    request, come from that request.
    """
    if event.scope != scope:
        return ()
    return tuple(
        item.key
        for item in event.evidence
        if item.provenance == "trusted"
        and item.scope == scope
        and item.candidate == candidate
        and item.purpose == purpose
        and (event.source_request is None or item.source_request == event.source_request)
    )


def ledger_refs(view: RunView, keys: tuple[EvidenceKey, ...]) -> tuple[EvidenceRef, ...] | None:
    """The ledger records for ``keys`` in order, or None when core holds any of them not."""
    ledger = {item.key: item for item in view.measurements}
    refs = tuple(ledger.get(key) for key in keys)
    if any(item is None for item in refs):
        return None
    return tuple(item for item in refs if item is not None)


def accept_readings(
    outcome: EvidenceReadings, requested: tuple[EvidenceRef, ...]
) -> tuple[AcceptedReading, ...] | str:
    """Bind each reading to the one requested record it decodes, or say why not.

    A reading must name a requested evidence ID that no other requested record
    shares, at most once, with the record's own kind, and may claim a pass only for
    a record whose observation succeeded.
    """
    accepted: list[AcceptedReading] = []
    for reading in outcome.readings:
        matches = tuple(item for item in requested if item.evidence_id == reading.evidence_id)
        if len(matches) != 1:
            return f"reading {reading.evidence_id.root} names no single requested record"
        (ref,) = matches
        if reading.kind is not ref.kind:
            return f"reading {reading.evidence_id.root} has kind {reading.kind.value}"
        if reading.passed and ref.status is not ObservationStatus.SUCCEEDED:
            return f"reading {reading.evidence_id.root} passed over a {ref.status.value} record"
        if any(item.evidence_id == reading.evidence_id for item in accepted):
            return f"reading {reading.evidence_id.root} repeated"
        accepted.append(AcceptedReading(**reading.model_dump(), source_request=ref.source_request))
    return tuple(accepted)


def turn_candidate(
    view: RunView, attempt: AttemptId, invocation: InvocationRef
) -> RevisionRef | None:
    """The revision this very turn retained, never an earlier turn's checkpoint.

    A retry turn that retained nothing new yields None even when the attempt holds
    older checkpoints, so it reports "retained no changed candidate".
    """
    live = next((item for item in view.attempts if item.attempt_id == attempt), None)
    if live is None:
        return None
    mine = tuple(item for item in live.checkpoints if item.invocation == invocation)
    return mine[-1].revision if mine else None
