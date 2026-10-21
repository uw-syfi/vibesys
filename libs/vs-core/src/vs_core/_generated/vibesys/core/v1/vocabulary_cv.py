# Generated from vibesys/core/v1/vocabulary.proto by protoc-gen-corevalue. DO NOT EDIT.
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


class DigestScheme(StrEnum):
    "Closed set of revision digest schemes; a digest is `<scheme>:<value>`."

    GIT_COMMIT = "git-commit"


class LifecycleClass(StrEnum):
    "Lifecycle class lifecycle contract."

    QUERY = "query"
    IDEMPOTENT_WRITE = "idempotent_external_write"
    OWNED_JOB = "owned_long_running_job"
    SESSION_TURN = "session_turn"


class EvidenceKind(StrEnum):
    "Closed measurement kinds used for eligibility requirements."

    CORRECTNESS = "correctness"
    BENCHMARK = "benchmark"
    PROFILING = "profiling"


class AssessmentKind(StrEnum):
    "Closed semantic assessment kinds, distinct from evidence production."

    CORRECTNESS = "correctness"
    BENCHMARK = "benchmark"
    PROFILING = "profiling"
    LOCAL_VALIDATION = "local-validation"


class ObservationStatus(StrEnum):
    "Observation status lifecycle contract."

    SUCCEEDED = "succeeded"
    PENDING = "pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ChargeKind(StrEnum):
    "Separate currencies with different admission and refund semantics.\n\nADMISSION charges accepted registration including queued starts. ATTEMPT\ncharges a paid invocation or failed setup cycle without an existing paid\ncharge. TURN charges one logical invocation, regardless of max_turns or\ncharge class, and never refunds."

    ADMISSION = "admission"
    ATTEMPT = "attempt"
    TURN = "turn"


class ComparisonVerdict(StrEnum):
    "Which side of the noise a candidate's result falls on, against its base.\n\n``INCONCLUSIVE`` is no side: the samples held give no estimate of the noise (too few runs a\nside, or runs that all read alike), so the ratio is neither a gain nor within the noise."

    BETTER = "better"
    WITHIN_NOISE = "within_noise"
    WORSE = "worse"
    INCONCLUSIVE = "inconclusive"


class EffectVerdict(StrEnum):
    "What a comparison says of a plan's expected effect, a minimum ratio over the base.\n\nThe framework derives it (`vs_core._comparison.effect_verdict`); no agent states it."

    SUPPORTED = "supported"
    WITHIN_NOISE = "within_noise"
    DISPROVEN = "disproven"
    INCONCLUSIVE = "inconclusive"


class Charge(StrEnum):
    "What a role's first turn in a session costs the run's budget.\n\nA turn that works on a candidate costs one ``ATTEMPT`` (and a turn); a turn that does not\n(a judge, a planner) costs only a ``TURN``. Corrections and resumes cost a turn."

    ATTEMPT = "paid"
    TURN = "free"


class ReplyErrorCode(StrEnum):
    "Why a reply did not validate against the agent's reply model. Closed."

    MISSING = "missing"
    UNEXPECTED = "unexpected"
    WRONG_TYPE = "wrong-type"
    INVALID_VALUE = "invalid-value"
    NOT_JSON = "not-json"
