"""Deterministic receipt-derived parent indexes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vs_evaluation.api import EvidenceKind, EvidenceOutcome, TrustedEvidence


class ParentComparisonKey(BaseModel):
    """Context in which observed partials are scientifically comparable.

    Workload fingerprints bind the captured workload/root facts. Receipt
    ``trusted_inputs`` binds candidate content and is deliberately excluded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    quantity: str
    unit: str | None
    direction: Literal["max", "min"]
    stage: str
    evaluator: str
    workload: str
    environment: str
    protocol: int
    target: float | None
    progress_unit: str | None
    progress_required: int | None


class ParentSnapshot(BaseModel):
    """An exact retained revision and its canonical accepted stage receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    hypothesis_id: str = Field(min_length=1)
    generation: int = Field(default=0, ge=0)
    revision: str = Field(min_length=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    handle_id: str = Field(min_length=1)
    submission_index: int = Field(ge=0)
    accuracy: TrustedEvidence
    benchmark: TrustedEvidence | None = None
    retained: bool = False
    change_summary: str | None = None
    artifact_refs: tuple[str, ...] = ()


class ParentCatalog(BaseModel):
    """Durable facts only; latest and best are derived rather than competing facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshots: tuple[ParentSnapshot, ...] = ()


class ParentOption(BaseModel):
    """A buildable exact snapshot with separate chronology and fitness labels."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    option_id: str
    snapshot: ParentSnapshot
    latest_verified: bool
    best_partial: bool
    comparison_key: ParentComparisonKey | None = None


def _eligible(snapshot: ParentSnapshot) -> bool:
    accuracy = snapshot.accuracy
    if not snapshot.retained or accuracy.kind is not EvidenceKind.ACCURACY:
        return False
    if accuracy.outcome is not EvidenceOutcome.PASSED:
        return False
    receipts = (accuracy,) if snapshot.benchmark is None else (accuracy, snapshot.benchmark)
    return all(
        item.evaluation_id == snapshot.handle_id
        and item.fingerprints.candidate.value == snapshot.content_digest
        and item.fingerprints == accuracy.fingerprints
        and item.trusted_inputs == accuracy.trusted_inputs
        for item in receipts
    ) and (snapshot.benchmark is None or snapshot.benchmark.kind is EvidenceKind.BENCHMARK)


def _identity(snapshot: ParentSnapshot) -> tuple[str, str, str]:
    return snapshot.hypothesis_id, snapshot.handle_id, snapshot.accuracy.evidence_id


def ingest(catalog: ParentCatalog, snapshot: ParentSnapshot) -> ParentCatalog:
    """Publish an eligible retained receipt idempotently; conflicting receipts raise.

    Ineligible observations supply no buildable parent. Input order and replay
    never change chronology or comparison identity.
    """
    if not _eligible(snapshot):
        return catalog
    existing = next(
        (item for item in catalog.snapshots if _identity(item) == _identity(snapshot)), None
    )
    if existing is not None:
        if existing == snapshot:
            return catalog
        if (
            snapshot.benchmark is None
            and existing.benchmark is not None
            and existing.model_copy(update={"benchmark": None}) == snapshot
        ):
            return catalog
        if existing.benchmark is not None and snapshot.benchmark is not None:
            message = "parent receipt identity conflicts with settled benchmark"
            raise ValueError(message)
        # Accuracy can settle before the benchmark. Only extend that exact
        # publication with its later receipt; never rewrite retained identity.
        if existing.model_copy(update={"benchmark": snapshot.benchmark}) != snapshot:
            message = "parent receipt identity conflicts with retained snapshot"
            raise ValueError(message)
    rows = tuple(item for item in catalog.snapshots if _identity(item) != _identity(snapshot))
    return ParentCatalog(snapshots=tuple(sorted((*rows, snapshot), key=_identity)))


def _comparison(snapshot: ParentSnapshot) -> ParentComparisonKey | None:
    benchmark = snapshot.benchmark
    if benchmark is None or benchmark.partial_measurement is None:
        return None
    partial = benchmark.partial_measurement
    if partial.unit is None:
        return None
    fingerprints = benchmark.fingerprints
    return ParentComparisonKey(
        quantity=partial.name,
        unit=partial.unit,
        direction=partial.direction,
        stage=benchmark.stage_name,
        evaluator=fingerprints.evaluator.value,
        workload=fingerprints.workload.value,
        environment=fingerprints.environment.value,
        protocol=benchmark.result_protocol,
        target=partial.target,
        progress_unit=partial.progress.unit if partial.progress is not None else None,
        progress_required=partial.progress.required if partial.progress is not None else None,
    )


def _partial_rank(snapshot: ParentSnapshot) -> tuple[float, float, str]:
    benchmark = snapshot.benchmark
    if benchmark is None or benchmark.partial_measurement is None:
        message = "partial ranking requires an accepted partial receipt"
        raise ValueError(message)
    partial = benchmark.partial_measurement
    value = partial.value if partial.direction == "max" else -partial.value
    progress = partial.progress
    completed = progress.completed / progress.required if progress is not None else -1.0
    return -value, -completed, benchmark.evidence_id


def options(catalog: ParentCatalog) -> tuple[ParentOption, ...]:
    """Offer every retained snapshot, labeling latest and each comparable best.

    Legacy zero ordinals do not establish latest chronology. Unlike quantities
    and execution contexts are never ranked together.
    """
    eligible = tuple(item for item in catalog.snapshots if _eligible(item))
    latest: dict[str, ParentSnapshot] = {}
    best: dict[ParentComparisonKey, ParentSnapshot] = {}
    for item in eligible:
        previous = latest.get(item.hypothesis_id)
        if item.submission_index > 0 and (
            previous is None
            or (item.submission_index, _identity(item))
            > (previous.submission_index, _identity(previous))
        ):
            latest[item.hypothesis_id] = item
        key = _comparison(item)
        if key is not None and (key not in best or _partial_rank(item) < _partial_rank(best[key])):
            best[key] = item
    return tuple(
        ParentOption(
            option_id=f"{item.hypothesis_id}:{item.revision}:{item.accuracy.evidence_id}",
            snapshot=item,
            latest_verified=latest.get(item.hypothesis_id) == item,
            best_partial=(key := _comparison(item)) is not None and best.get(key) == item,
            comparison_key=key,
        )
        for item in sorted(eligible, key=_presentation_order)
    )


def _presentation_order(
    snapshot: ParentSnapshot,
) -> tuple[str, tuple[float, float, str], tuple[str, str, str]]:
    key = _comparison(snapshot)
    if key is None:
        return "~accuracy-only", (0.0, 0.0, ""), _identity(snapshot)
    return key.model_dump_json(), _partial_rank(snapshot), _identity(snapshot)


def resolve(
    catalog: ParentCatalog, hypothesis_id: str, revision: str | None = None
) -> ParentSnapshot | None:
    """Resolve an exact selector; omission means latest known verified receipt.

    Unknown or mismatched selectors return None, never base. A caller owning
    legacy snapshot policy may resolve that separately when chronology is unknown.
    """
    matches = tuple(row for row in options(catalog) if row.snapshot.hypothesis_id == hypothesis_id)
    if revision is None:
        return next((row.snapshot for row in matches if row.latest_verified), None)
    return next((row.snapshot for row in matches if row.snapshot.revision == revision), None)
