"""Strict contracts for the agent evaluation socket boundary."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from vs_evaluation.agent_evidence import EvidenceKind
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
