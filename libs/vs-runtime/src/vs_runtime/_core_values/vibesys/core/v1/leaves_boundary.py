# Generated from vibesys/core/v1/leaves.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api.values as values
from vs_proto.api import core_leaves as pb
from vs_runtime._core_values.vibesys.core.v1.common_boundary import (
    decode_EvidenceId,
    decode_RequestId,
    decode_RevisionId,
    decode_Scope,
    encode_EvidenceId,
    encode_RequestId,
    encode_RevisionId,
    encode_Scope,
)
from vs_runtime._core_values.vibesys.core.v1.vocabulary_boundary import (
    decode_ComparisonVerdict,
    encode_ComparisonVerdict,
)
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def encode_Direction(v: Literal["max", "min"] | None) -> pb.Direction:
    return {"max": pb.Direction.MAX, "min": pb.Direction.MIN}.get(v, pb.Direction.UNSPECIFIED)


def decode_Direction(v: pb.Direction) -> Literal["max", "min"]:
    return cast(Literal["max", "min"], {1: "max", 2: "min"}[v])


def encode_ChargeClass(v: Literal["free", "paid", "correction", "resume"] | None) -> pb.ChargeClass:
    return {
        "free": pb.ChargeClass.FREE,
        "paid": pb.ChargeClass.PAID,
        "correction": pb.ChargeClass.CORRECTION,
        "resume": pb.ChargeClass.RESUME,
    }.get(v, pb.ChargeClass.UNSPECIFIED)


def decode_ChargeClass(v: pb.ChargeClass) -> Literal["free", "paid", "correction", "resume"]:
    return cast(
        Literal["free", "paid", "correction", "resume"],
        {1: "free", 2: "paid", 3: "correction", 4: "resume"}[v],
    )


def encode_TaskObjectiveDirection(v: Literal["max", "min"] | None) -> pb.TaskObjectiveDirection:
    return {"max": pb.TaskObjectiveDirection.MAX, "min": pb.TaskObjectiveDirection.MIN}.get(
        v, pb.TaskObjectiveDirection.UNSPECIFIED
    )


def decode_TaskObjectiveDirection(v: pb.TaskObjectiveDirection) -> Literal["max", "min"]:
    return cast(Literal["max", "min"], {1: "max", 2: "min"}[v])


def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError("Required protobuf message is absent")
    return value


def encode_EvidenceKey(v: values.EvidenceKey) -> pb.EvidenceKey:
    return pb.EvidenceKey(
        kind=v.kind,
        source_request=encode_RequestId(v.source_request),
        evidence_id=encode_EvidenceId(v.evidence_id),
    )


def decode_EvidenceKey(m: pb.EvidenceKey) -> values.EvidenceKey:
    return values.EvidenceKey(
        kind=cast(Literal["evidence_key"], m.kind),
        source_request=decode_RequestId(_required(m.source_request)),
        evidence_id=decode_EvidenceId(_required(m.evidence_id)),
    )


def encode_RevisionRef(v: values.RevisionRef) -> pb.RevisionRef:
    return pb.RevisionRef(revision_id=encode_RevisionId(v.revision_id), digest=v.digest)


def decode_RevisionRef(m: pb.RevisionRef) -> values.RevisionRef:
    return values.RevisionRef(
        revision_id=decode_RevisionId(_required(m.revision_id)), digest=m.digest
    )


def encode_TaskStage(v: values.TaskStage) -> pb.TaskStage:
    return pb.TaskStage(stage_id=v.stage_id, execution_seconds=v.execution_seconds)


def decode_TaskStage(m: pb.TaskStage) -> values.TaskStage:
    return values.TaskStage(stage_id=m.stage_id, execution_seconds=m.execution_seconds)


def encode_TaskObjective(v: values.TaskObjective) -> pb.TaskObjective:
    return pb.TaskObjective(name=v.name, direction=encode_TaskObjectiveDirection(v.direction))


def decode_TaskObjective(m: pb.TaskObjective) -> values.TaskObjective:
    return values.TaskObjective(name=m.name, direction=decode_TaskObjectiveDirection(m.direction))


def encode_Reading(v: values.Reading) -> pb.Reading:
    return pb.Reading(
        metric=v.metric,
        value=v.value,
        direction=encode_Direction(v.direction),
        unit=v.unit,
        repetitions=str(v.repetitions),
    )


def decode_Reading(m: pb.Reading) -> values.Reading:
    return values.Reading(
        metric=m.metric,
        value=m.value,
        direction=None if m.direction == 0 else decode_Direction(m.direction),
        unit=m.unit if m.has_field("unit") else None,
        repetitions=int(m.repetitions),
    )


def encode_BaseRef(v: values.BaseRef) -> pb.BaseRef:
    return pb.BaseRef(commit=encode_RevisionRef(v.commit), measured_input=v.measured_input)


def decode_BaseRef(m: pb.BaseRef) -> values.BaseRef:
    return values.BaseRef(
        commit=decode_RevisionRef(_required(m.commit)), measured_input=m.measured_input
    )


def encode_Sample(v: values.Sample) -> pb.Sample:
    return pb.Sample(
        run=encode_RequestId(v.run),
        scope=encode_Scope(v.scope),
        commit=encode_RevisionRef(v.commit),
        measured_input=v.measured_input,
        evaluation=v.evaluation,
        gates=[x for x in v.gates],
        host=v.host,
        dedicated_resources=v.dedicated_resources,
        reading=encode_Reading(v.reading),
        observed_at=v.observed_at,
        base=None if v.base is None else encode_BaseRef(v.base),
    )


def decode_Sample(m: pb.Sample) -> values.Sample:
    return values.Sample(
        run=decode_RequestId(_required(m.run)),
        scope=decode_Scope(_required(m.scope)),
        commit=decode_RevisionRef(_required(m.commit)),
        measured_input=m.measured_input,
        evaluation=m.evaluation,
        gates=tuple(x for x in m.gates),
        host=m.host,
        dedicated_resources=m.dedicated_resources,
        reading=decode_Reading(_required(m.reading)),
        observed_at=m.observed_at,
        base=None if m.base is None else decode_BaseRef(m.base),
    )


def encode_Comparison(v: values.Comparison) -> pb.Comparison:
    return pb.Comparison(
        scope=encode_Scope(v.scope),
        metric=v.metric,
        unit=v.unit,
        direction=encode_Direction(v.direction),
        candidate=encode_RevisionRef(v.candidate),
        candidate_input=v.candidate_input,
        base=encode_RevisionRef(v.base),
        base_input=v.base_input,
        candidate_value=v.candidate_value,
        base_value=v.base_value,
        ratio=v.ratio,
        noise=v.noise,
        verdict=encode_ComparisonVerdict(v.verdict),
        candidate_samples=str(v.candidate_samples),
        base_samples=str(v.base_samples),
    )


def decode_Comparison(m: pb.Comparison) -> values.Comparison:
    return values.Comparison(
        scope=decode_Scope(_required(m.scope)),
        metric=m.metric,
        unit=m.unit if m.has_field("unit") else None,
        direction=decode_Direction(m.direction),
        candidate=decode_RevisionRef(_required(m.candidate)),
        candidate_input=m.candidate_input,
        base=decode_RevisionRef(_required(m.base)),
        base_input=m.base_input,
        candidate_value=m.candidate_value,
        base_value=m.base_value,
        ratio=m.ratio,
        noise=m.noise,
        verdict=decode_ComparisonVerdict(m.verdict),
        candidate_samples=int(m.candidate_samples),
        base_samples=int(m.base_samples),
    )


ENCODERS: dict[type, Callable[..., Message]] = {
    values.EvidenceKey: encode_EvidenceKey,
    values.RevisionRef: encode_RevisionRef,
    values.TaskStage: encode_TaskStage,
    values.TaskObjective: encode_TaskObjective,
    values.Reading: encode_Reading,
    values.BaseRef: encode_BaseRef,
    values.Sample: encode_Sample,
    values.Comparison: encode_Comparison,
}
DECODERS: dict[type, Callable[..., object]] = {
    pb.EvidenceKey: decode_EvidenceKey,
    pb.RevisionRef: decode_RevisionRef,
    pb.TaskStage: decode_TaskStage,
    pb.TaskObjective: decode_TaskObjective,
    pb.Reading: decode_Reading,
    pb.BaseRef: decode_BaseRef,
    pb.Sample: decode_Sample,
    pb.Comparison: decode_Comparison,
}
