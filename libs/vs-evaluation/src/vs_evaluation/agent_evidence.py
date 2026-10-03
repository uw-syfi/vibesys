"""Semantic evidence contracts used at the agent evaluation boundary.

These models identify optimization facts accepted from completed stages.
Product orchestration supplies the adapter that validates and stores them;
this module keeps the private agent protocol independent of that policy.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vs_evaluator_protocol.api import PartialMeasurement


class EvidenceKind(StrEnum):
    """Semantic facts available through the evaluation agent service."""

    ACCURACY = "accuracy"
    BENCHMARK = "benchmark"
    PROFILE = "profile"


class EvidenceOutcome(StrEnum):
    """Typed conclusion carried by accepted evidence."""

    PASSED = "passed"
    FAILED = "failed"
    OBSERVED = "observed"


class ContentDigest(BaseModel):
    """A content address used for exact candidate and evaluator identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["sha256"] = "sha256"
    value: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def sha256(cls, content: bytes) -> Self:
        """Create the canonical digest for *content*."""
        return cls(value=hashlib.sha256(content).hexdigest())


class EvidenceFingerprints(BaseModel):
    """Immutable identity of one reusable evaluation fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: ContentDigest
    evaluator: ContentDigest
    workload: ContentDigest
    environment: ContentDigest


class EvidenceMetric(BaseModel):
    """One named finite measurement in an accepted result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    value: FiniteFloat
    direction: Literal["max", "min"] | None = None
    unit: str | None = None


class ArtifactDigest(BaseModel):
    """One content-addressed artifact relative to its evaluation root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    digest: ContentDigest


class TrustedEvidence(BaseModel):
    """A stage result already accepted by the host's trust policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_id: str = Field(min_length=1)
    stage_name: str = Field(min_length=1)
    result_protocol: Literal[1] = 1
    kind: EvidenceKind
    fingerprints: EvidenceFingerprints
    trusted_inputs: ContentDigest
    outcome: EvidenceOutcome
    semantic_summary: str | None = Field(default=None, max_length=16_384)
    metrics: tuple[EvidenceMetric, ...] = ()
    # What a failed stage measured before it stopped, as its evaluator
    # reported it through the evaluator result protocol; never inferred.
    partial_measurement: PartialMeasurement | None = None
    artifacts: tuple[ArtifactDigest, ...] = ()
    accepted_round: int = Field(ge=0)


__all__ = [
    "ArtifactDigest",
    "ContentDigest",
    "EvidenceFingerprints",
    "EvidenceKind",
    "EvidenceMetric",
    "EvidenceOutcome",
    "PartialMeasurement",
    "TrustedEvidence",
]
