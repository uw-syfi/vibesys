# Generated from vibesys/core/v1/vocabulary.proto by protoc-gen-corevalue. DO NOT EDIT.
from __future__ import annotations

from typing import Callable, Literal, cast

from protobuf import Message, Oneof

import vs_core.api.values as values
from vs_proto.api import core_vocabulary as pb
from vs_runtime._corevalue_wkt import decode_timestamp, encode_timestamp


def encode_DigestScheme(v: values.DigestScheme | None) -> pb.DigestScheme:
    return {"git-commit": pb.DigestScheme.GIT_COMMIT}.get(v, pb.DigestScheme.UNSPECIFIED)


def decode_DigestScheme(v: pb.DigestScheme) -> values.DigestScheme:
    return values.DigestScheme({1: "git-commit"}[v])


def encode_LifecycleClass(v: values.LifecycleClass | None) -> pb.LifecycleClass:
    return {
        "query": pb.LifecycleClass.QUERY,
        "idempotent_external_write": pb.LifecycleClass.IDEMPOTENT_WRITE,
        "owned_long_running_job": pb.LifecycleClass.OWNED_JOB,
        "session_turn": pb.LifecycleClass.SESSION_TURN,
    }.get(v, pb.LifecycleClass.UNSPECIFIED)


def decode_LifecycleClass(v: pb.LifecycleClass) -> values.LifecycleClass:
    return values.LifecycleClass(
        {
            1: "query",
            2: "idempotent_external_write",
            3: "owned_long_running_job",
            4: "session_turn",
        }[v]
    )


def encode_EvidenceKind(v: values.EvidenceKind | None) -> pb.EvidenceKind:
    return {
        "correctness": pb.EvidenceKind.CORRECTNESS,
        "benchmark": pb.EvidenceKind.BENCHMARK,
        "profiling": pb.EvidenceKind.PROFILING,
    }.get(v, pb.EvidenceKind.UNSPECIFIED)


def decode_EvidenceKind(v: pb.EvidenceKind) -> values.EvidenceKind:
    return values.EvidenceKind({1: "correctness", 2: "benchmark", 3: "profiling"}[v])


def encode_AssessmentKind(v: values.AssessmentKind | None) -> pb.AssessmentKind:
    return {
        "correctness": pb.AssessmentKind.CORRECTNESS,
        "benchmark": pb.AssessmentKind.BENCHMARK,
        "profiling": pb.AssessmentKind.PROFILING,
        "local-validation": pb.AssessmentKind.LOCAL_VALIDATION,
    }.get(v, pb.AssessmentKind.UNSPECIFIED)


def decode_AssessmentKind(v: pb.AssessmentKind) -> values.AssessmentKind:
    return values.AssessmentKind(
        {1: "correctness", 2: "benchmark", 3: "profiling", 4: "local-validation"}[v]
    )


def encode_ObservationStatus(v: values.ObservationStatus | None) -> pb.ObservationStatus:
    return {
        "succeeded": pb.ObservationStatus.SUCCEEDED,
        "pending": pb.ObservationStatus.PENDING,
        "cancelled": pb.ObservationStatus.CANCELLED,
        "rejected": pb.ObservationStatus.REJECTED,
        "failed": pb.ObservationStatus.FAILED,
        "unknown": pb.ObservationStatus.UNKNOWN,
    }.get(v, pb.ObservationStatus.UNSPECIFIED)


def decode_ObservationStatus(v: pb.ObservationStatus) -> values.ObservationStatus:
    return values.ObservationStatus(
        {1: "succeeded", 2: "pending", 3: "cancelled", 4: "rejected", 5: "failed", 6: "unknown"}[v]
    )


def encode_ChargeKind(v: values.ChargeKind | None) -> pb.ChargeKind:
    return {
        "admission": pb.ChargeKind.ADMISSION,
        "attempt": pb.ChargeKind.ATTEMPT,
        "turn": pb.ChargeKind.TURN,
    }.get(v, pb.ChargeKind.UNSPECIFIED)


def decode_ChargeKind(v: pb.ChargeKind) -> values.ChargeKind:
    return values.ChargeKind({1: "admission", 2: "attempt", 3: "turn"}[v])


def encode_ComparisonVerdict(v: values.ComparisonVerdict | None) -> pb.ComparisonVerdict:
    return {
        "better": pb.ComparisonVerdict.BETTER,
        "within_noise": pb.ComparisonVerdict.WITHIN_NOISE,
        "worse": pb.ComparisonVerdict.WORSE,
        "inconclusive": pb.ComparisonVerdict.INCONCLUSIVE,
    }.get(v, pb.ComparisonVerdict.UNSPECIFIED)


def decode_ComparisonVerdict(v: pb.ComparisonVerdict) -> values.ComparisonVerdict:
    return values.ComparisonVerdict(
        {1: "better", 2: "within_noise", 3: "worse", 4: "inconclusive"}[v]
    )


def encode_EffectVerdict(v: values.EffectVerdict | None) -> pb.EffectVerdict:
    return {
        "supported": pb.EffectVerdict.SUPPORTED,
        "within_noise": pb.EffectVerdict.WITHIN_NOISE,
        "disproven": pb.EffectVerdict.DISPROVEN,
        "inconclusive": pb.EffectVerdict.INCONCLUSIVE,
    }.get(v, pb.EffectVerdict.UNSPECIFIED)


def decode_EffectVerdict(v: pb.EffectVerdict) -> values.EffectVerdict:
    return values.EffectVerdict(
        {1: "supported", 2: "within_noise", 3: "disproven", 4: "inconclusive"}[v]
    )


def encode_Charge(v: values.Charge | None) -> pb.Charge:
    return {"paid": pb.Charge.ATTEMPT, "free": pb.Charge.TURN}.get(v, pb.Charge.UNSPECIFIED)


def decode_Charge(v: pb.Charge) -> values.Charge:
    return values.Charge({1: "paid", 2: "free"}[v])


def encode_ReplyErrorCode(v: values.ReplyErrorCode | None) -> pb.ReplyErrorCode:
    return {
        "missing": pb.ReplyErrorCode.MISSING,
        "unexpected": pb.ReplyErrorCode.UNEXPECTED,
        "wrong-type": pb.ReplyErrorCode.WRONG_TYPE,
        "invalid-value": pb.ReplyErrorCode.INVALID_VALUE,
        "not-json": pb.ReplyErrorCode.NOT_JSON,
    }.get(v, pb.ReplyErrorCode.UNSPECIFIED)


def decode_ReplyErrorCode(v: pb.ReplyErrorCode) -> values.ReplyErrorCode:
    return values.ReplyErrorCode(
        {1: "missing", 2: "unexpected", 3: "wrong-type", 4: "invalid-value", 5: "not-json"}[v]
    )


def encode_CheckoutStatus(v: values.CheckoutStatus | None) -> pb.CheckoutStatus:
    return {
        "fast_forwarded": pb.CheckoutStatus.FAST_FORWARDED,
        "other_branch": pb.CheckoutStatus.OTHER_BRANCH,
        "dirty_tree": pb.CheckoutStatus.DIRTY_TREE,
        "diverged": pb.CheckoutStatus.DIVERGED,
        "git_refused": pb.CheckoutStatus.GIT_REFUSED,
        "operator_changed_checkout": pb.CheckoutStatus.OPERATOR_CHANGED_CHECKOUT,
    }.get(v, pb.CheckoutStatus.UNSPECIFIED)


def decode_CheckoutStatus(v: pb.CheckoutStatus) -> values.CheckoutStatus:
    return values.CheckoutStatus(
        {
            1: "fast_forwarded",
            2: "other_branch",
            3: "dirty_tree",
            4: "diverged",
            5: "git_refused",
            6: "operator_changed_checkout",
        }[v]
    )


def _required[T](value: T | None) -> T:
    if value is None:
        raise ValueError("Required protobuf message is absent")
    return value


ENCODERS: dict[type, Callable[..., Message]] = {}
DECODERS: dict[type, Callable[..., object]] = {}
