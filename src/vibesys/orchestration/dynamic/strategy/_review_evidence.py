"""The trusted evaluations a judge is shown for the candidate it reviews.

An agent measures its own workspace through the evaluation tool; core records the
result in its evidence ledger as trusted `local-validation` evidence of the attempt.
The ledger holds identities only, so the strategy asks the evidence owner to decode
them (`InterpretEvidence`) before the judge's prompt is built, and the prompt then
carries each record's evidence ID, kind, revision, verdict, metrics and failure tail
as typed data. The judge never searches the filesystem for results.
"""

from vibesys.orchestration.dynamic.strategy._evidence import accept_readings
from vibesys.orchestration.dynamic.strategy._operations import EvidenceReadings
from vibesys.orchestration.dynamic.strategy._prompts import ReviewEvaluation
from vibesys.orchestration.dynamic.strategy._rows import AcceptedReading, ReviewEvidence
from vibesys.orchestration.dynamic.strategy._state import AttemptRecord
from vs_core.api import EvidenceKey, EvidenceRef, RunView

_FAILURE_TAIL_CHARS = 1500
"""A failure's cause is usually stated last, so a long one keeps only its end."""


def review_refs(view: RunView, record: AttemptRecord) -> tuple[EvidenceRef, ...]:
    """Trusted agent-submitted evidence of the attempt's candidate, oldest first."""
    if record.candidate is None:
        return ()
    held = (
        item
        for item in view.measurements
        if item.provenance == "trusted"
        and item.purpose == "local-validation"
        and item.candidate == record.candidate
        and item.scope.owner == record.attempt
        and item.scope.generation == record.generation
    )
    return tuple(sorted(held, key=lambda item: item.observation_sequence))


def decoded(record: AttemptRecord, refs: tuple[EvidenceRef, ...]) -> ReviewEvidence | None:
    """The record's readings when they decode exactly ``refs`` of its candidate, else None."""
    held = record.review_evidence
    if held is None or held.candidate != record.candidate:
        return None
    return held if held.keys == tuple(item.key for item in refs) else None


def awaits_reading(record: AttemptRecord, view: RunView) -> tuple[EvidenceRef, ...]:
    """The evidence the evidence owner must still decode before the judge is asked."""
    refs = review_refs(view, record)
    return () if not refs or decoded(record, refs) is not None else refs


def with_readings(record: AttemptRecord, view: RunView, outcome: EvidenceReadings) -> AttemptRecord:
    """Keep what the owner decoded; a refusal leaves the evidence listed without numbers."""
    refs = review_refs(view, record)
    accepted = accept_readings(outcome, refs) if outcome.status == "succeeded" else ()
    readings = () if isinstance(accepted, str) else accepted
    if record.candidate is None:
        return record
    return record.model_copy(
        update={
            "review_evidence": ReviewEvidence(
                candidate=record.candidate,
                keys=tuple(item.key for item in refs),
                readings=readings,
            )
        }
    )


def evaluations(record: AttemptRecord, view: RunView) -> tuple[ReviewEvaluation, ...]:
    """Each trusted evaluation record of the candidate, with its reading when decoded."""
    refs = review_refs(view, record)
    held = decoded(record, refs)
    readings: dict[EvidenceKey, AcceptedReading] = (
        {} if held is None else {item.key: item for item in held.readings}
    )
    return tuple(_evaluation(ref, readings.get(ref.key)) for ref in refs)


def _evaluation(ref: EvidenceRef, reading: AcceptedReading | None) -> ReviewEvaluation:
    feedback = "" if reading is None else reading.feedback
    return ReviewEvaluation(
        evidence_id=ref.evidence_id,
        kind=ref.kind,
        revision=ref.candidate,
        status=ref.status,
        passed=None if reading is None else reading.passed,
        metrics=() if reading is None else reading.metrics,
        partial=None if reading is None else reading.partial,
        feedback=feedback[-_FAILURE_TAIL_CHARS:],
        feedback_cut=len(feedback) > _FAILURE_TAIL_CHARS,
    )
