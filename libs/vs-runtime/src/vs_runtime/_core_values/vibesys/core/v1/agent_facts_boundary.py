# Generated from vibesys/core/v1/agent_facts.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api.values as values
from vs_proto.api import core_agent_facts as pb
from vs_runtime._core_values.vibesys.core.v1.common_boundary import (
    decode_ArtifactRef,
    decode_EvidenceId,
    decode_RequestId,
    decode_Scope,
    encode_ArtifactRef,
    encode_EvidenceId,
    encode_RequestId,
    encode_Scope,
)
from vs_runtime._core_values.vibesys.core.v1.leaves_boundary import (
    decode_Reading,
    decode_RevisionRef,
    encode_Reading,
    encode_RevisionRef,
)
from vs_runtime._core_values.vibesys.core.v1.vocabulary_boundary import (
    decode_EvidenceKind,
    decode_ObservationStatus,
    encode_EvidenceKind,
    encode_ObservationStatus,
)
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def encode_EvidencePurpose(
    v: Literal["baseline", "local-validation", "official", "profile"] | None,
) -> pb.EvidencePurpose:
    return {
        "baseline": pb.EvidencePurpose.BASELINE,
        "local-validation": pb.EvidencePurpose.LOCAL_VALIDATION,
        "official": pb.EvidencePurpose.OFFICIAL,
        "profile": pb.EvidencePurpose.PROFILE,
    }.get(v, pb.EvidencePurpose.UNSPECIFIED)


def decode_EvidencePurpose(
    v: pb.EvidencePurpose,
) -> Literal["baseline", "local-validation", "official", "profile"]:
    return cast(
        Literal["baseline", "local-validation", "official", "profile"],
        {1: "baseline", 2: "local-validation", 3: "official", 4: "profile"}[v],
    )


def encode_EvidenceProvenance(v: Literal["trusted", "self-report"] | None) -> pb.EvidenceProvenance:
    return {
        "trusted": pb.EvidenceProvenance.TRUSTED,
        "self-report": pb.EvidenceProvenance.SELF_REPORT,
    }.get(v, pb.EvidenceProvenance.UNSPECIFIED)


def decode_EvidenceProvenance(v: pb.EvidenceProvenance) -> Literal["trusted", "self-report"]:
    return cast(Literal["trusted", "self-report"], {1: "trusted", 2: "self-report"}[v])


def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError("Required protobuf message is absent")
    return value


def encode_EvidenceAcceptanceReceipt(
    v: values.EvidenceAcceptanceReceipt,
) -> pb.EvidenceAcceptanceReceipt:
    return pb.EvidenceAcceptanceReceipt(
        source_request=encode_RequestId(v.source_request),
        scope=encode_Scope(v.scope),
        observation_sequence=str(v.observation_sequence),
        status=encode_ObservationStatus(v.status),
    )


def decode_EvidenceAcceptanceReceipt(
    m: pb.EvidenceAcceptanceReceipt,
) -> values.EvidenceAcceptanceReceipt:
    return values.EvidenceAcceptanceReceipt(
        source_request=decode_RequestId(_required(m.source_request)),
        scope=decode_Scope(_required(m.scope)),
        observation_sequence=int(m.observation_sequence),
        status=cast(
            Literal[
                values.ObservationStatus.SUCCEEDED,
                values.ObservationStatus.PENDING,
                values.ObservationStatus.CANCELLED,
                values.ObservationStatus.REJECTED,
                values.ObservationStatus.FAILED,
            ],
            decode_ObservationStatus(m.status),
        ),
    )


def encode_EvidenceRef(v: values.EvidenceRef) -> pb.EvidenceRef:
    return pb.EvidenceRef(
        evidence_id=encode_EvidenceId(v.evidence_id),
        kind=encode_EvidenceKind(v.kind),
        purpose=encode_EvidencePurpose(v.purpose),
        scope=encode_Scope(v.scope),
        source_request=encode_RequestId(v.source_request),
        candidate=encode_RevisionRef(v.candidate),
        observation_sequence=str(v.observation_sequence),
        evaluator_digest=v.evaluator_digest,
        workload_digest=v.workload_digest,
        environment_digest=v.environment_digest,
        provenance=encode_EvidenceProvenance(v.provenance),
        status=encode_ObservationStatus(v.status),
        artifacts=[encode_ArtifactRef(x) for x in v.artifacts],
        acceptance_receipt=None
        if v.acceptance_receipt is None
        else encode_EvidenceAcceptanceReceipt(v.acceptance_receipt),
        reused_from=None if v.reused_from is None else encode_RequestId(v.reused_from),
        reading=None if v.reading is None else encode_Reading(v.reading),
        unmet_requirement=v.unmet_requirement,
    )


def decode_EvidenceRef(m: pb.EvidenceRef) -> values.EvidenceRef:
    return values.EvidenceRef(
        evidence_id=decode_EvidenceId(_required(m.evidence_id)),
        kind=decode_EvidenceKind(m.kind),
        purpose=decode_EvidencePurpose(m.purpose),
        scope=decode_Scope(_required(m.scope)),
        source_request=decode_RequestId(_required(m.source_request)),
        candidate=decode_RevisionRef(_required(m.candidate)),
        observation_sequence=int(m.observation_sequence),
        evaluator_digest=m.evaluator_digest,
        workload_digest=m.workload_digest,
        environment_digest=m.environment_digest,
        provenance=decode_EvidenceProvenance(m.provenance),
        status=decode_ObservationStatus(m.status),
        artifacts=tuple(decode_ArtifactRef(x) for x in m.artifacts),
        acceptance_receipt=None
        if m.acceptance_receipt is None
        else decode_EvidenceAcceptanceReceipt(m.acceptance_receipt),
        reused_from=None if m.reused_from is None else decode_RequestId(m.reused_from),
        reading=None if m.reading is None else decode_Reading(m.reading),
        unmet_requirement=m.unmet_requirement if m.has_field("unmet_requirement") else None,
    )


ENCODERS: dict[type, Callable[..., Message]] = {
    values.EvidenceAcceptanceReceipt: encode_EvidenceAcceptanceReceipt,
    values.EvidenceRef: encode_EvidenceRef,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.EvidenceAcceptanceReceipt: decode_EvidenceAcceptanceReceipt,
    pb.EvidenceRef: decode_EvidenceRef,
}
