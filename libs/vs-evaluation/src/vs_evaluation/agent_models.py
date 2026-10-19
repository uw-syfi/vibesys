"""Strict contracts for the agent evaluation socket boundary."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    model_validator,
)

from vs_evaluation.agent_evidence import (
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    PartialMeasurement,
    TrustedEvidence,
)
from vs_evaluation.models import ProfileField


class AgentToolArgs(BaseModel):
    """The fields an agent supplies to one evaluation tool.

    Each wire call subclasses its tool's arguments and adds the host-held
    ``action`` and ``token``, so the schema an agent is offered and the model
    the service validates are one definition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


def _unique_kinds(kinds: tuple[EvidenceKind, ...]) -> tuple[EvidenceKind, ...]:
    if len(kinds) != len(set(kinds)):
        message = "evidence kinds must be unique"
        raise ValueError(message)
    return kinds


EvidenceKinds = Annotated[
    tuple[EvidenceKind, ...],
    AfterValidator(_unique_kinds),
    # The validator is invisible to JSON schema; state the same rule there.
    Field(json_schema_extra={"uniqueItems": True}),
]


class EvidenceKindsArgs(AgentToolArgs):
    """Arguments naming the semantic evidence kinds a tool applies to."""

    evidence_kinds: EvidenceKinds = Field(
        default=(),
        description="Requested semantic evidence kinds. Empty means every kind granted to this role.",
    )


class AdditionalProfileCaptureReason(BaseModel):
    """Typed justification for measuring facts an existing capture lacks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["missing_facts"] = "missing_facts"
    source_handle: str = Field(min_length=1)
    missing_facts: tuple[ProfileField, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _nonblank_facts(self) -> AdditionalProfileCaptureReason:
        if len(set(self.missing_facts)) != len(self.missing_facts):
            message = "missing_facts must be unique"
            raise ValueError(message)
        return self


class SubmitArgs(EvidenceKindsArgs):
    """Submission intent, including explicit justification for an extra profile."""

    additional_capture_reason: AdditionalProfileCaptureReason | None = None


class SubmitCall(SubmitArgs):
    """Submit one or more semantic evidence stages."""

    action: Literal["submit"] = "submit"
    token: str


class WaitArgs(AgentToolArgs):
    """Evaluation handles to authorize before yielding a turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    handles: tuple[str, ...] = Field(min_length=1)


class WaitCall(WaitArgs):
    """Validate every dependency against the calling principal's authority."""

    action: Literal["validate_wait"] = "validate_wait"
    token: str


class WaitReply(BaseModel):
    """All requested handles authorize a structured evaluation yield."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["wait_validated"] = "wait_validated"
    handles: tuple[str, ...]


MAX_STAGE_SUMMARY_TAIL_CHARS = 600


class EvaluationStageOutcome(BaseModel):
    """The trusted conclusion of one stage that recorded evidence.

    ``outcome`` is the stage's verdict: a benchmark that ran but missed its
    requirement is ``failed`` even though its evidence was recorded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: EvidenceKind
    outcome: EvidenceOutcome
    metrics: tuple[EvidenceMetric, ...] = ()
    # What a failed stage measured before it stopped, as its evaluator reported it.
    partial_measurement: PartialMeasurement | None = None
    # The end of the stage's own summary, where a failure states its cause.
    summary_tail: str | None = Field(default=None, max_length=MAX_STAGE_SUMMARY_TAIL_CHARS)
    # The accepted evidence record this conclusion reads, when it came from one.
    evidence_id: str | None = None

    @classmethod
    def of(cls, evidence: TrustedEvidence) -> EvaluationStageOutcome:
        """The one projection of an accepted record that agents see.

        Every agent-visible statement of a stage's result (the evaluation tool's reply, the
        evaluation snapshot) is built here from the record, so a field added to the record
        reaches them together and no consumer copies values by hand.
        """
        summary = evidence.semantic_summary
        return cls(
            kind=evidence.kind,
            outcome=evidence.outcome,
            metrics=evidence.metrics,
            partial_measurement=evidence.partial_measurement,
            summary_tail=summary[-MAX_STAGE_SUMMARY_TAIL_CHARS:] if summary else None,
            evidence_id=evidence.evidence_id,
        )


class EvaluationComparison(BaseModel):
    """The framework's comparison of a candidate with its base, as the agent is told it.

    Every number is the framework's: the candidate's mean over its samples and the base's mean
    over its own, their ``ratio`` (candidate over base), the ``noise`` (one standard error of
    that ratio) and the ``verdict`` it reached. ``better`` and ``worse`` need the difference to
    exceed a critical number of noises in the metric's direction (``max`` or ``min`` is
    better): two under a task's declared per-run noise; without one, Welch's t test at 95%,
    and no verdict until each side holds ``MIN_SAMPLES_WITHOUT_FLOOR`` (3) runs that do not
    all read alike (the rule is in ``vs_core._comparison``). Anything closer is
    ``within_noise``. Until then the verdict is ``inconclusive``: the samples held give no
    estimate of the noise, so the ratio says nothing yet (it is not noise), and the framework
    takes more runs of both sides itself while a run can still change that. ``base_commit`` is the revision the base was measured at. More runs of
    the same candidate content add samples and narrow the noise. It describes the samples held
    when it is given: a candidate is ranked by the value stored at its official measurement,
    which later runs do not move. ``settling`` says it is provisional: a run of either side
    that will feed it has not ended, so it may still change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    metric: str = Field(min_length=1)
    unit: str | None = None
    direction: Literal["max", "min"]
    candidate_value: FiniteFloat
    base_value: FiniteFloat
    ratio: FiniteFloat
    noise: FiniteFloat = Field(ge=0)
    verdict: Literal["better", "within_noise", "worse", "inconclusive"]
    base_commit: str = Field(min_length=1)
    candidate_samples: int = Field(ge=1)
    base_samples: int = Field(ge=1)
    settling: bool = Field(
        description=(
            "Whether the comparison is provisional: a run of the candidate or of its base that "
            "will add a sample is still under way, so the comparison may change when it lands. "
            "Evaluate the unchanged content again later to read it then."
        )
    )


class SubmittedReply(BaseModel):
    """Nonblocking submission receipt.

    ``repeat`` is set when the run already held an evaluation of this exact candidate (same
    content and measurement policy) in this workspace's scope: the handle is that
    evaluation's, nothing was charged, and whichever role of the scope submitted it first.
    ``outcome`` names how it ended (``succeeded``, ``failed``, ...) when it has; it is None
    while the evaluation runs. A finished evaluation needs no wait: ``evidence`` lists the
    ids of the records it produced, as the resume of a wait would, and ``stages`` carries what
    they measured, so the agent never has to guess a number from an opaque id.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["submitted"] = "submitted"
    handle_id: str
    repeat: bool = False
    outcome: str | None = Field(default=None, min_length=1)
    evidence: tuple[str, ...] = ()
    stages: tuple[EvaluationStageOutcome, ...] = Field(
        default=(),
        description=(
            "What each finished stage measured: its verdict, every metric with value, unit "
            "and direction, and the end of its summary, generated from the trusted record."
        ),
    )
    comparison: EvaluationComparison | None = Field(
        default=None,
        description=(
            "How the finished candidate measured against its base: both values, the ratio, "
            "the noise, the framework's verdict and the base commit, over the runs held now. "
            "Absent while the evaluation runs, and while no fresh run of the base has ended. "
            "A candidate is ranked by the value stored at its official measurement; runs "
            "after that move this comparison, not the ranked value."
        ),
    )


class RunStoppingReply(BaseModel):
    """The run is stopping, so the request started no new work.

    Returned for a new evaluation submission or profiler dispatch after a stop
    is requested. Nothing was submitted and no handle exists; end the turn.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["run_stopping"] = "run_stopping"
    instruction: str = Field(
        default=(
            "The run is stopping: no evaluation or profile was started. "
            "Do not submit more work; finish this turn now."
        ),
        min_length=1,
    )


class SocketSuccess(BaseModel):
    """Successful strict socket response envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: Literal[True] = True
    result: JsonValue


class SocketFailure(BaseModel):
    """Rejected strict socket response envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: Literal[False] = False
    error: str


SocketReply = Annotated[SocketSuccess | SocketFailure, Field(discriminator="ok")]


RECEIPT_LINE = b"received\n"
"""What a client sends after reading a complete reply, before it closes its connection.

A host that charged a submission keeps it owed to the agent until this line arrives: a
reply written to a connection the client already abandoned (its tool deadline expired)
reached nobody.
"""

AgentEvaluationCall = Annotated[SubmitCall | WaitCall, Field(discriminator="action")]
AgentEvaluationReply = Annotated[
    SubmittedReply | RunStoppingReply | WaitReply, Field(discriminator="kind")
]


__all__ = [
    "RECEIPT_LINE",
    "AgentEvaluationCall",
    "AgentEvaluationReply",
    "AgentToolArgs",
    "EvidenceKinds",
    "EvidenceKindsArgs",
    "RunStoppingReply",
    "SocketFailure",
    "SocketReply",
    "SocketSuccess",
    "SubmitCall",
    "SubmittedReply",
    "WaitArgs",
    "WaitCall",
    "WaitReply",
]
