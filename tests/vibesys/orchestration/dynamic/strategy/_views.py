"""Constructed core `RunView`s and evidence for strategy tests (public core API only)."""

from __future__ import annotations

from vibesys.orchestration.dynamic.strategy.api import (
    EvidenceReading,
    MetricRow,
    ParentSnapshot,
    PartialRow,
)
from vs_core.api import (
    AttemptId,
    AttemptRef,
    EvidenceId,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    RunView,
    Scope,
    Settlement,
    SettlementId,
    project,
)
from vs_core.testing.builders import initial_state


def empty_view() -> RunView:
    """A capability-disabled run with no attempts or evidence."""
    return project(initial_state())


def revision(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"digest-{name}")


def evidence_ref(
    evidence_id: str,
    kind: EvidenceKind,
    candidate: RevisionRef,
    *,
    sequence: int = 1,
    digest: str = "x",
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=EvidenceId(root=evidence_id),
        kind=kind,
        purpose="official",
        scope=Scope(owner=RunId(root="run"), generation=0),
        source_request=RequestId(root=f"request-{evidence_id}"),
        candidate=candidate,
        observation_sequence=sequence,
        evaluator_digest=f"evaluator-{digest}",
        workload_digest=f"workload-{digest}",
        environment_digest=f"environment-{digest}",
        provenance="trusted",
        status=ObservationStatus.SUCCEEDED,
    )


def partial(value: float, *, completed: int = 71, direction: str = "max") -> PartialRow:
    return PartialRow(
        name="throughput",
        value=value,
        direction=direction,
        unit="tokens/s",
        completed=completed,
        required=72,
        progress_unit="rounds",
    )


def snapshot(value: float, ordinal: int, *, hypothesis: str = "source") -> ParentSnapshot:
    rev = revision(f"revision-{ordinal}")
    return ParentSnapshot(
        hypothesis_id=hypothesis,
        revision=rev,
        submission_index=ordinal,
        accuracy=EvidenceReading(
            evidence_id=EvidenceId(root=f"accuracy-{ordinal}"),
            kind=EvidenceKind.CORRECTNESS,
            passed=True,
            stage="accuracy",
        ),
        benchmark=EvidenceReading(
            evidence_id=EvidenceId(root=f"benchmark-{ordinal}"),
            kind=EvidenceKind.BENCHMARK,
            passed=False,
            stage="benchmark",
            partial=partial(value, completed=71 if ordinal == 1 else 65),
            metrics=(MetricRow(name="throughput", value=value, direction="max"),),
        ),
    )


def view_proving(*snapshots: ParentSnapshot, retained: bool = True) -> RunView:
    """A view whose evidence ledger and retained checkpoints prove the snapshots."""
    base = empty_view()
    measurements: list[EvidenceRef] = []
    for item in snapshots:
        measurements.append(
            evidence_ref(item.accuracy.evidence_id.root, EvidenceKind.CORRECTNESS, item.revision)
        )
        if item.benchmark is not None:
            measurements.append(
                evidence_ref(item.benchmark.evidence_id.root, EvidenceKind.BENCHMARK, item.revision)
            )
    settlements = tuple(
        Settlement(
            settlement_id=SettlementId(root=f"settlement-{item.revision.revision_id.root}"),
            attempt=AttemptRef(attempt_id=AttemptId(root=f"attempt-{index}"), generation=0),
            candidate=item.revision,
            assessments=(),
            eligible=False,
            retention="candidate",
            outcome="succeeded",
        )
        for index, item in enumerate(snapshots)
        if retained
    )
    return base.model_copy(update={"measurements": tuple(measurements), "settlements": settlements})
