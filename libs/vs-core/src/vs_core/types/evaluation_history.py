"""Pure attempt-wide evaluation history and continuation bound evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ContractValidationError,
    Count,
    Observation,
    ObservationStatus,
    RequestId,
    Scope,
    Value,
)


class EvaluationHistoryAvailability(StrEnum):
    """Unavailable history cannot prove an empty or successful history."""

    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"


class EvaluationStageOutcome(StrEnum):
    """Scientific stage result, independent of external execution success."""

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class EvaluationStageResult(Value):
    """One validated stage result from the accepted terminal submission fact."""

    stage_id: str = Field(min_length=1)
    outcome: EvaluationStageOutcome


class BenchmarkFailure(Value):
    """Failed benchmark partial and the explicit comparison interval containing it.

    The measurement owner validates the rate and interval against its typed
    outcome. Absence of this value cannot prove a repeated measurement failure.
    """

    partial_rate: float = Field(ge=0, allow_inf_nan=False)
    rate_lower: float = Field(ge=0, allow_inf_nan=False)
    rate_upper: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def contained_rate(self) -> BenchmarkFailure:
        """Reject reversed intervals and partials outside their comparison range."""
        if not self.rate_lower <= self.partial_rate <= self.rate_upper:
            raise ContractValidationError("partial_rate", "outside declared rate range")
        return self


class EvaluationHistoryCursor(Value):
    """Immutable preceding-submission cursor captured at resume publication.

    Zero identifies the beginning; otherwise submission_id names the exact
    ordinal. A missing ID never stands in for a nonempty historical cursor.
    """

    ordinal: Count = 0
    submission_id: RequestId | None = None

    @model_validator(mode="after")
    def exact_cursor(self) -> EvaluationHistoryCursor:
        """Require identity precisely when the cursor names a submission."""
        if (self.ordinal == 0) != (self.submission_id is None):
            raise ContractValidationError("submission_id", "must match cursor ordinal")
        return self


class EvaluationTerminalFacts(Value):
    """Owner-normalized scientific facts from one conclusive terminal observation.

    Executors supply values, not history ordinals or coverage authority. Intents A
    forwards these unchanged to Measurements A, which validates canonical source,
    scope and stage identities before constructing the durable history record.
    Absent facts on accepted execution mean history is unavailable, never empty.
    """

    stages: tuple[EvaluationStageResult, ...] = ()
    traceback_signature: str | None = Field(default=None, min_length=1)
    failed_benchmark: BenchmarkFailure | None = None
    accuracy_passed: bool = False

    @model_validator(mode="after")
    def distinct_stages(self) -> EvaluationTerminalFacts:
        """A stage has exactly one scientific result in its terminal payload."""
        if len({stage.stage_id for stage in self.stages}) != len(self.stages):
            raise ContractValidationError("stages", "duplicate stage ID")
        return self

    def validate_observation(self, observation: Observation) -> None:
        """Scientific execution facts require accepted conclusive terminal source.

        External observation carriers call this at their strict ingress boundary.
        Positive nonacceptance preserves history accounting without this payload.
        """
        if (
            not observation.accepted
            or not observation.terminal
            or observation.status in (ObservationStatus.PENDING, ObservationStatus.UNKNOWN)
        ):
            raise ContractValidationError(
                "evaluation_result", "requires accepted conclusive terminal observation"
            )


class AttemptEvaluationRecord(EvaluationTerminalFacts):
    """One ordered attempt submission with its conclusive terminal fact.

    Measurement A appends once after validating canonical submission, scope,
    stages and typed outcome. The observation is retained unchanged; a later
    job update cannot rewrite traceback, partial benchmark or accuracy facts.
    Accuracy success can reset a failure streak only when accuracy_passed is
    positively true. Diagnostic text alone is never a traceback signature.
    """

    ordinal: int = Field(ge=1)
    submission_id: RequestId
    scope: Scope
    terminal_observation: Observation

    @model_validator(mode="after")
    def terminal_receipt(self) -> AttemptEvaluationRecord:
        """Require exact terminal source and unique stage identities."""
        observation = self.terminal_observation
        if observation.request_id != self.submission_id or observation.scope != self.scope:
            raise ContractValidationError("terminal_observation", "submission/scope mismatch")
        if not observation.terminal or observation.status in (
            ObservationStatus.PENDING,
            ObservationStatus.UNKNOWN,
        ):
            raise ContractValidationError(
                "terminal_observation", "requires conclusive terminal fact"
            )
        if not observation.accepted and (
            observation.status
            not in (
                ObservationStatus.FAILED,
                ObservationStatus.REJECTED,
                ObservationStatus.CANCELLED,
            )
            or self.stages
            or self.traceback_signature is not None
            or self.failed_benchmark is not None
            or self.accuracy_passed
        ):
            raise ContractValidationError(
                "terminal_observation", "nonacceptance cannot prove scientific execution facts"
            )
        return self


class AttemptEvaluationHistory(Value):
    """Ordered durable records plus explicit owner-issued coverage manifest.

    Attempts A stores updates issued by Measurements A. COMPLETE requires every
    prepared submission for the attempt through covered_submissions to have
    exactly one record, in preparation order. The leaf checks this manifest
    against canonical submission receipts; absence defaults to UNAVAILABLE,
    including migration, and can never authorize another evaluation or resume.
    """

    availability: EvaluationHistoryAvailability = EvaluationHistoryAvailability.UNAVAILABLE
    covered_submissions: tuple[RequestId, ...] = ()
    records: tuple[AttemptEvaluationRecord, ...] = ()

    @model_validator(mode="after")
    def ordered_coverage(self) -> AttemptEvaluationHistory:
        """Reject duplicates, holes and falsely complete submission coverage."""
        ids = tuple(record.submission_id for record in self.records)
        if len(set(ids)) != len(ids):
            raise ContractValidationError("records", "duplicate submission ID")
        ordinals = tuple(record.ordinal for record in self.records)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ContractValidationError("records", "ordinals must be unique and increasing")
        if len(set(self.covered_submissions)) != len(self.covered_submissions):
            raise ContractValidationError("covered_submissions", "duplicate submission ID")
        if any(
            record.ordinal > len(self.covered_submissions)
            or self.covered_submissions[record.ordinal - 1] != record.submission_id
            for record in self.records
        ):
            raise ContractValidationError("records", "submission differs from coverage ordinal")
        if self.availability == EvaluationHistoryAvailability.COMPLETE and (
            ids != self.covered_submissions or ordinals != tuple(range(1, len(ids) + 1))
        ):
            raise ContractValidationError(
                "covered_submissions", "complete history differs from records"
            )
        return self

    @property
    def cursor(self) -> EvaluationHistoryCursor:
        """Project the last prepared identity without claiming terminal coverage."""
        if not self.covered_submissions:
            return EvaluationHistoryCursor()
        return EvaluationHistoryCursor(
            ordinal=len(self.covered_submissions), submission_id=self.covered_submissions[-1]
        )


class AttemptTerminalReason(StrEnum):
    """Durable continuation exhaustion, distinct from resource release."""

    REPEATED_TRACEBACK = "repeated-traceback"
    REPEATED_MEASUREMENT = "repeated-measurement"
    HISTORY_UNAVAILABLE = "history-unavailable"
    NO_NEW_EVALUATION = "no-new-evaluation"


class RepeatedFailureGuidance(Value):
    """Typed facts for strategy templates; no prompt text or dispatch authority."""

    reason: Literal[
        AttemptTerminalReason.REPEATED_TRACEBACK, AttemptTerminalReason.REPEATED_MEASUREMENT
    ]
    consecutive_failures: int = Field(ge=1)
    limit: int = Field(ge=1)
    cursor: EvaluationHistoryCursor
    traceback_signature: str | None = Field(default=None, min_length=1)
    failed_benchmark: BenchmarkFailure | None = None

    @model_validator(mode="after")
    def reason_evidence(self) -> RepeatedFailureGuidance:
        """Require the failure fact and enough recorded submissions for its streak."""
        if self.consecutive_failures > self.cursor.ordinal:
            raise ContractValidationError("consecutive_failures", "exceeds preceding submissions")
        if (
            self.reason == AttemptTerminalReason.REPEATED_TRACEBACK
            and self.traceback_signature is None
        ):
            raise ContractValidationError("traceback_signature", "required for repeated traceback")
        if (
            self.reason == AttemptTerminalReason.REPEATED_MEASUREMENT
            and self.failed_benchmark is None
        ):
            raise ContractValidationError("failed_benchmark", "required for repeated measurement")
        return self
