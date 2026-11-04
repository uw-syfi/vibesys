"""Lossless lowering of the runtime's accepted stage ledger into shared facts."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vs_core.api.values import Reading
from vs_proto.api import core_agent_facts, core_leaves
from vs_proto.api import imported_facts as pb
from vs_runtime.api.core_values import to_proto

if TYPE_CHECKING:
    from vs_evaluator_protocol.api import PartialMeasurement
    from vs_runtime.api.core import EvidenceEntry


def stage_measurement(entry: EvidenceEntry) -> pb.ImportedMeasurement:
    evidence = entry.evidence
    fingerprints = evidence.fingerprints
    stage = pb.ImportedStageEvidence(
        evidence_id=evidence.evidence_id,
        evaluation_id=evidence.evaluation_id,
        stage_name=evidence.stage_name,
        kind=pb.ImportedStageKind[evidence.kind.name],
        purpose=core_agent_facts.EvidencePurpose[entry.purpose.upper().replace("-", "_")],
        candidate_digest=fingerprints.candidate.value,
        evaluator_digest=fingerprints.evaluator.value,
        workload_digest=fingerprints.workload.value,
        environment_digest=fingerprints.environment.value,
        trusted_inputs_digest=evidence.trusted_inputs.value,
        outcome=pb.ImportedStageOutcome[evidence.outcome.name],
        readings=[
            cast(
                "core_leaves.Reading",
                to_proto(
                    Reading(
                        metric=metric.name,
                        value=metric.value,
                        unit=metric.unit,
                        direction=metric.direction,
                    )
                ),
            )
            for metric in evidence.metrics
        ],
        summary=evidence.semantic_summary,
        artifacts=[
            pb.ImportedEvidenceArtifact(path=artifact.path, sha256=artifact.digest.value)
            for artifact in evidence.artifacts
        ],
        partial=None
        if evidence.partial_measurement is None
        else partial_measurement(evidence.partial_measurement),
        failure_kind=None
        if evidence.failure_kind is None
        else pb.ImportedEvidenceFailure[evidence.failure_kind.name],
        unmet_requirement=evidence.unmet_requirement,
        accepted_round=str(evidence.accepted_round),
    )
    return pb.ImportedMeasurement(stage_evidence=stage)


def partial_measurement(value: PartialMeasurement) -> pb.ImportedPartialMeasurement:
    progress = value.progress
    return pb.ImportedPartialMeasurement(
        reading=cast(
            "core_leaves.Reading",
            to_proto(
                Reading(
                    metric=value.name, value=value.value, direction=value.direction, unit=value.unit
                )
            ),
        ),
        target=value.target,
        completed=None if progress is None else str(progress.completed),
        required=None if progress is None else str(progress.required),
        unit=None if progress is None else progress.unit,
    )
