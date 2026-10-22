"""Trusted semantic identity carried through an executor-specific codec."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, ValidationError

from vs_evaluation.agent_evidence import (
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    PartialMeasurement,
)
from vs_evaluator_protocol.api import ProfileField

if TYPE_CHECKING:
    from vs_evaluation.models import EvaluationRequest


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
    purpose: str | None = Field(default=None, min_length=1)
    """What the caller measures this stage for, as it names it; not part of its work.

    Neither :func:`evidence_identity` nor :func:`same_work` reads it, so a request
    recorded before stages carried a purpose is the same work as one that names it.

    An executor passes it on to whoever shows the stage's output, so a host can tell,
    for example, a local validation's accuracy stage from an official one.
    """


@dataclass(frozen=True, kw_only=True)
class EvidenceResultIdentity:
    """Semantic result content, including its immutable operation attribution."""

    evaluation_id: str
    outcome: EvidenceOutcome
    summary: str | None
    metrics: tuple[EvidenceMetric, ...]
    partial: PartialMeasurement | None
    unmet_requirement: str | None = None


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
    if result.unmet_requirement is not None:
        identity["unmet_requirement"] = result.unmet_requirement
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


_NOT_WORK = frozenset({"purpose"})
"""The fields of a :class:`SemanticEvaluationStage` that say why it runs, not what it measures."""


def same_work(recorded: EvaluationRequest, request: EvaluationRequest) -> bool:
    """Whether ``request`` asks for the work ``recorded`` asked for.

    Requests are compared whole, except that each semantic stage is compared without
    its purpose. A stage payload that is no semantic stage is compared as it is.
    """
    return _work(recorded) == _work(request)


def evaluation_work_digest(request: EvaluationRequest) -> str:
    """Canonical receipt identity using the existing semantic work normalization."""
    content = json.dumps(
        _work(request).model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(content.encode()).hexdigest()


def _work(request: EvaluationRequest) -> EvaluationRequest:
    return request.model_copy(
        update={
            "stages": tuple(
                step.model_copy(update={"payload": _stage_work(step.payload)})
                for step in request.stages
            )
        }
    )


def _stage_work(payload: JsonValue) -> JsonValue:
    try:
        stage = SemanticEvaluationStage.model_validate(payload)
    except ValidationError:
        return payload
    return stage.model_dump(mode="json", exclude=set(_NOT_WORK))
