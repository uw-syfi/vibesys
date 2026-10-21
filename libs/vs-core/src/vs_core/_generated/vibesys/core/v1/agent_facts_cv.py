# Generated from vibesys/core/v1/agent_facts.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from enum import StrEnum
from functools import partial
from typing import Annotated, ClassVar, Literal, final

from pydantic import AfterValidator, ConfigDict, Field, field_serializer
from pydantic.dataclasses import dataclass

from vs_core._corevalue import (
    CoreValue,
    FrozenMap,
    TimestampValue,
    bounded_integer,
    cel_integer,
    omit_unset,
    resolve_value,
)
from vs_core._generated.vibesys.core.v1.common_cv import ArtifactRef, EvidenceId, RequestId, Scope
from vs_core._generated.vibesys.core.v1.leaves_cv import Reading, RevisionRef
from vs_core._generated.vibesys.core.v1.vocabulary_cv import EvidenceKind, ObservationStatus

EvidencePurpose = Literal["baseline", "local-validation", "official", "profile"]

EvidenceProvenance = Literal["trusted", "self-report"]


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class EvidenceAcceptanceReceipt(CoreValue):
    "Immutable positive acceptance proof, without job lifecycle or admission state."

    model_fields: ClassVar[tuple[str, ...]] = (
        "source_request",
        "scope",
        "observation_sequence",
        "status",
    )
    source_request: RequestId
    scope: Scope
    observation_sequence: Annotated[int, Field(ge=0)]
    status: Literal[
        ObservationStatus.SUCCEEDED,
        ObservationStatus.PENDING,
        ObservationStatus.CANCELLED,
        ObservationStatus.REJECTED,
        ObservationStatus.FAILED,
    ]

    __hash__ = CoreValue.cached_hash


@final
@dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    config=ConfigDict(extra="forbid", strict=True, validate_default=True),
)
class EvidenceRef(CoreValue):
    "Immutable EvidenceRef value."

    model_fields: ClassVar[tuple[str, ...]] = (
        "evidence_id",
        "kind",
        "purpose",
        "scope",
        "source_request",
        "candidate",
        "observation_sequence",
        "evaluator_digest",
        "workload_digest",
        "environment_digest",
        "provenance",
        "status",
        "artifacts",
        "acceptance_receipt",
        "reused_from",
        "reading",
        "unmet_requirement",
    )
    evidence_id: EvidenceId
    kind: EvidenceKind
    purpose: EvidencePurpose
    scope: Scope
    source_request: RequestId
    candidate: RevisionRef
    observation_sequence: Annotated[int, Field(ge=0)]
    evaluator_digest: str
    workload_digest: str
    environment_digest: str
    provenance: EvidenceProvenance
    status: ObservationStatus
    artifacts: tuple[ArtifactRef, ...] = ()
    acceptance_receipt: EvidenceAcceptanceReceipt | None = None
    reused_from: Annotated[RequestId | None, Field(exclude_if=omit_unset)] = None
    reading: Annotated[Reading | None, Field(exclude_if=omit_unset)] = None
    unmet_requirement: Annotated[str | None, Field(exclude_if=omit_unset)] = None

    def __post_init__(self) -> None:
        """Enforce the proto message rules at construction as well as decode."""
        if not (
            not (self.acceptance_receipt is not None)
            or (
                self.acceptance_receipt.source_request == self.source_request
                and self.acceptance_receipt.scope == self.scope
                and str(self.acceptance_receipt.observation_sequence)
                == str(self.observation_sequence)
                and {
                    "succeeded": 1,
                    "pending": 2,
                    "cancelled": 3,
                    "rejected": 4,
                    "failed": 5,
                    "unknown": 6,
                }.get(self.acceptance_receipt.status, 0)
                == {
                    "succeeded": 1,
                    "pending": 2,
                    "cancelled": 3,
                    "rejected": 4,
                    "failed": 5,
                    "unknown": 6,
                }.get(self.status, 0)
            )
        ):
            raise ValueError(
                "acceptance_receipt: source, scope, sequence and status must match evidence"
            )

    __hash__ = CoreValue.cached_hash


resolve_value(EvidenceAcceptanceReceipt)
resolve_value(EvidenceRef)
