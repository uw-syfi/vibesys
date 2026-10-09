"""Trusted semantic identity carried through an executor-specific codec."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue

from vs_evaluation.agent_evidence import (
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    PartialMeasurement,
)
from vs_evaluator_protocol.api import ProfileField


class SemanticEvaluationStage(BaseModel):
    """Trusted semantic identity carried through an executor-specific codec."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: str
    kind: EvidenceKind
    required_profile_fields: tuple[ProfileField, ...] = ()
    fingerprints: EvidenceFingerprints
    submitted_at_s: FiniteFloat | None = Field(default=None, ge=0)
    deadline_at_s: FiniteFloat | None = Field(default=None, ge=0)
    deadline_unavailable: str | None = Field(default=None, min_length=1)


@dataclass(frozen=True, kw_only=True)
class EvidenceResultIdentity:
    """Semantic result content, including its immutable operation attribution."""

    evaluation_id: str
    outcome: EvidenceOutcome
    summary: str | None
    metrics: tuple[EvidenceMetric, ...]
    partial: PartialMeasurement | None


def evidence_identity(stage: SemanticEvaluationStage, result: EvidenceResultIdentity) -> str:
    """Return the content address of one stage's attributed semantic result.

    Operation attribution is part of the content: identical measurements from
    distinct evaluations must not share an ID with conflicting evaluation_id.
    Existing recorded IDs are read without recomputation.
    """
    identity: dict[str, JsonValue] = {
        "evaluation_id": result.evaluation_id,
        "kind": stage.kind.value,
        "fingerprints": stage.fingerprints.model_dump(mode="json"),
        "outcome": result.outcome.value,
        "summary": result.summary,
        "metrics": [item.model_dump(mode="json") for item in result.metrics],
    }
    if result.partial is not None:
        identity["partial_measurement"] = result.partial.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
