"""Parent options projected from core evidence and retained-revision receipts.

The catalog stores only strategy labels and choices: which hypothesis produced a
revision, which accepted evidence IDs prove it, the decoded readings and a
chronology ordinal. Whether a revision is *buildable* is always re-derived from
`RunView`: the exact revision must be retained by core and its accepted accuracy
evidence must name exactly that revision. No second evidence ledger exists.

Observed partials are compared only within a matching quantity, unit, direction,
stage, evaluator, workload, environment, protocol, target and progress contract.
Fitness never conveys adoption authority: a failed-benchmark partial is offered
as a parent but is never winner-eligible.
"""

from typing import TYPE_CHECKING, Literal

from vibesys.orchestration.dynamic.strategy._rows import AcceptedReading
from vs_core.api import (
    EvidenceKey,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    RevisionRef,
    RunView,
    Value,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


class ParentConflictError(ValueError):
    """A receipt identity was republished with different facts."""


class ComparisonKey(Value):
    """Context in which observed partials are scientifically comparable."""

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


class ParentSnapshot(Value):
    """An exact retained revision and the decoded accepted readings that prove it."""

    hypothesis_id: str
    generation: int = 0
    revision: RevisionRef
    accuracy: AcceptedReading
    benchmark: AcceptedReading | None = None
    # Chronology of the producing evaluation; zero means unknown, never "latest".
    submission_index: int = 0
    change_summary: str | None = None


class ParentOption(Value):
    """A buildable exact snapshot with separate chronology and fitness labels."""

    option_id: str
    snapshot: ParentSnapshot
    latest_verified: bool
    best_partial: bool
    comparison_key: ComparisonKey | None = None


def _evidence(view: RunView) -> dict[EvidenceKey, EvidenceRef]:
    return {item.key: item for item in view.measurements}


def retained_revisions(view: RunView) -> frozenset[RevisionRef]:
    """Every revision core retained: attempt checkpoints and candidate settlements."""
    held = {checkpoint.revision for attempt in view.attempts for checkpoint in attempt.checkpoints}
    held.update(
        item.candidate
        for item in view.settlements
        if item.candidate is not None and item.retention != "discard"
    )
    return frozenset(held)


def _names_revision(evidence: EvidenceRef | None, revision: RevisionRef) -> bool:
    return evidence is not None and evidence.candidate == revision


def eligible(snapshot: ParentSnapshot, view: RunView) -> bool:
    """Whether core proves this snapshot is retained and its accuracy passed exactly."""
    ledger = _evidence(view)
    accuracy = ledger.get(snapshot.accuracy.key)
    if (
        snapshot.revision not in retained_revisions(view)
        or snapshot.accuracy.kind is not EvidenceKind.CORRECTNESS
        or not snapshot.accuracy.passed
        or not _names_revision(accuracy, snapshot.revision)
        or accuracy is None
        or accuracy.status is not ObservationStatus.SUCCEEDED
        or accuracy.provenance != "trusted"
    ):
        return False
    benchmark = snapshot.benchmark
    if benchmark is None:
        return True
    proof = ledger.get(benchmark.key)
    return (
        benchmark.kind is EvidenceKind.BENCHMARK
        and proof is not None
        and _names_revision(proof, snapshot.revision)
        and proof.provenance == "trusted"
        and (proof.evaluator_digest, proof.workload_digest, proof.environment_digest)
        == (accuracy.evaluator_digest, accuracy.workload_digest, accuracy.environment_digest)
    )


def _label(reading: AcceptedReading) -> str:
    return f"{reading.source_request.root}/{reading.evidence_id.root}"


def _identity(snapshot: ParentSnapshot) -> tuple[str, str, str]:
    """Producer, exact revision (id and digest) and accuracy evidence key."""
    return (
        snapshot.hypothesis_id,
        f"{snapshot.revision.revision_id.root}@{snapshot.revision.digest}",
        _label(snapshot.accuracy),
    )


def ingest(
    catalog: tuple[ParentSnapshot, ...], snapshot: ParentSnapshot, view: RunView
) -> tuple[ParentSnapshot, ...]:
    """Publish an eligible snapshot idempotently; conflicting receipts raise.

    A later benchmark reading may extend the same accuracy publication, never
    rewrite its retained identity. Input order and replay never change the result.
    """
    if not eligible(snapshot, view):
        return catalog
    existing = next((item for item in catalog if _identity(item) == _identity(snapshot)), None)
    if existing is not None:
        if existing == snapshot or (
            snapshot.benchmark is None
            and existing.model_copy(update={"benchmark": None}) == snapshot
        ):
            return catalog
        if existing.benchmark is not None and snapshot.benchmark is not None:
            message = "parent receipt identity conflicts with settled benchmark"
            raise ParentConflictError(message)
        if existing.model_copy(update={"benchmark": snapshot.benchmark}) != snapshot:
            message = "parent receipt identity conflicts with retained snapshot"
            raise ParentConflictError(message)
    rows = tuple(item for item in catalog if _identity(item) != _identity(snapshot))
    return tuple(sorted((*rows, snapshot), key=_identity))


def comparison_key(snapshot: ParentSnapshot, view: RunView) -> ComparisonKey | None:
    """The comparison context of a snapshot's partial, or None without authority."""
    benchmark = snapshot.benchmark
    if benchmark is None or benchmark.partial is None or benchmark.partial.unit is None:
        return None
    proof = _evidence(view).get(benchmark.key)
    if proof is None:
        return None
    partial = benchmark.partial
    return ComparisonKey(
        quantity=partial.name,
        unit=partial.unit,
        direction=partial.direction,
        stage=benchmark.stage,
        evaluator=proof.evaluator_digest,
        workload=proof.workload_digest,
        environment=proof.environment_digest,
        protocol=benchmark.protocol,
        target=partial.target,
        progress_unit=partial.progress_unit,
        progress_required=partial.required,
    )


def _partial_rank(snapshot: ParentSnapshot) -> tuple[float, float, str]:
    benchmark = snapshot.benchmark
    if benchmark is None or benchmark.partial is None:
        message = "partial ranking requires an accepted partial reading"
        raise ValueError(message)
    partial = benchmark.partial
    value = partial.value if partial.direction == "max" else -partial.value
    completed = (
        partial.completed / partial.required
        if partial.completed is not None and partial.required is not None
        else -1.0
    )
    return -value, -completed, _label(benchmark)


def _presentation(
    snapshot: ParentSnapshot, key: ComparisonKey | None
) -> tuple[str, tuple[float, float, str], tuple[str, str, str]]:
    if key is None:
        return "~accuracy-only", (0.0, 0.0, ""), _identity(snapshot)
    return key.model_dump_json(), _partial_rank(snapshot), _identity(snapshot)


def options(catalog: "Iterable[ParentSnapshot]", view: RunView) -> tuple[ParentOption, ...]:
    """Offer every buildable snapshot, labeling latest and each comparable best."""
    rows = tuple(item for item in catalog if eligible(item, view))
    keys = {_identity(item): comparison_key(item, view) for item in rows}
    latest: dict[str, ParentSnapshot] = {}
    best: dict[ComparisonKey, ParentSnapshot] = {}
    for item in rows:
        previous = latest.get(item.hypothesis_id)
        if item.submission_index > 0 and (
            previous is None
            or (item.submission_index, _identity(item))
            > (previous.submission_index, _identity(previous))
        ):
            latest[item.hypothesis_id] = item
        key = keys[_identity(item)]
        if key is not None and (key not in best or _partial_rank(item) < _partial_rank(best[key])):
            best[key] = item
    return tuple(
        ParentOption(
            option_id=(
                f"{item.hypothesis_id}:{item.revision.revision_id.root}:{_label(item.accuracy)}"
            ),
            snapshot=item,
            latest_verified=latest.get(item.hypothesis_id) == item,
            best_partial=(key := keys[_identity(item)]) is not None and best.get(key) == item,
            comparison_key=key,
        )
        for item in sorted(rows, key=lambda row: _presentation(row, keys[_identity(row)]))
    )


def resolve(
    catalog: "Iterable[ParentSnapshot]",
    view: RunView,
    hypothesis_id: str,
    revision_id: str | None = None,
) -> ParentSnapshot | None:
    """Resolve an exact selector; omission means the latest known verified receipt.

    Unknown or mismatched selectors return None, never the baseline.
    """
    matches = tuple(
        row for row in options(catalog, view) if row.snapshot.hypothesis_id == hypothesis_id
    )
    if revision_id is None:
        return next((row.snapshot for row in matches if row.latest_verified), None)
    return next(
        (row.snapshot for row in matches if row.snapshot.revision.revision_id.root == revision_id),
        None,
    )
