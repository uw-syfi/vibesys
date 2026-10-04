"""Assessment finality, retention eligibility and separate adoption."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .common import (
    AttemptRef,
    EvidenceId,
    InvocationRef,
    Observation,
    RequestBase,
    RevisionRef,
    SettlementId,
    Value,
)


class AssessmentProposal(Value):
    """Assessment proposal lifecycle contract."""

    verdict: Literal["satisfied", "rejected", "deferred"]
    sources: tuple[InvocationRef | EvidenceId, ...]
    candidate: RevisionRef | None
    schema_version: int = Field(ge=1)


class EvidenceRequirements(Value):
    """Evidence requirements lifecycle contract."""

    trusted_measurement: bool = False
    satisfied_assessment: bool = False
    allow_empty_queue_success: bool = False


class Settlement(Value):
    """Settlement lifecycle contract."""

    settlement_id: SettlementId
    attempt: AttemptRef
    candidate: RevisionRef | None
    assessments: tuple[AssessmentProposal, ...]
    eligible: bool
    retention: Literal["discard", "wip", "candidate"]
    outcome: Literal["succeeded", "failed", "cancelled", "blocked"]


class RetainedCandidate(Value):
    """Retained candidate lifecycle contract."""

    kind: Literal["retained_candidate"] = "retained_candidate"
    settlement_id: SettlementId
    revision: RevisionRef


class TrustedBaseline(Value):
    """Trusted baseline lifecycle contract."""

    kind: Literal["trusted_baseline"] = "trusted_baseline"
    revision: RevisionRef


type Selection = Annotated[RetainedCandidate | TrustedBaseline, Field(discriminator="kind")]


class RunResultProposal(Value):
    """Run result proposal lifecycle contract."""

    outcome: Literal["success", "failure", "blocked", "cancelled"]
    reason: str = Field(min_length=1)
    selection: Selection | None = None


class Adoption(Value):
    """Adoption lifecycle contract."""

    selection: Selection
    observation: Observation | None = None
    verified: bool = False


class SettlementState(Value):
    """Settlement state lifecycle contract."""

    settlements: tuple[Settlement, ...] = ()
    pending: tuple[Settlement, ...] = ()
    adoption: Adoption | None = None


class AssessmentSubmitted(Value):
    """Assessment submitted lifecycle contract."""

    kind: Literal["assessment_submitted"] = "assessment_submitted"
    settlement: Settlement


class OwnershipSettled(Value):
    """Ownership settled lifecycle contract."""

    kind: Literal["ownership_settled"] = "ownership_settled"
    attempt: AttemptRef
    released: bool
    blocked: bool = False


class WinnerProposed(Value):
    """Winner proposed lifecycle contract."""

    kind: Literal["winner_proposed"] = "winner_proposed"
    selection: Selection


class AdoptionObserved(Value):
    """Adoption observed lifecycle contract."""

    kind: Literal["adoption_observed"] = "adoption_observed"
    observation: Observation
    revision: RevisionRef | None


class AttemptSettled(Value):
    """Attempt settled lifecycle contract."""

    kind: Literal["attempt_settled"] = "attempt_settled"
    settlement: Settlement


class AdoptionResult(Value):
    """Adoption result lifecycle contract."""

    kind: Literal["adoption_result"] = "adoption_result"
    selection: Selection
    observation: Observation


class AdoptRevision(RequestBase):
    """Adopt revision lifecycle contract."""

    kind: Literal["adopt_revision"] = "adopt_revision"
    selection: Selection


class VerifyAdoption(RequestBase):
    """Verify adoption lifecycle contract."""

    kind: Literal["verify_adoption"] = "verify_adoption"
    selection: Selection


type SettlementEvent = Annotated[
    AssessmentSubmitted | OwnershipSettled | WinnerProposed | AdoptionObserved | AttemptSettled,
    Field(discriminator="kind"),
]
type AdoptionRequest = Annotated[AdoptRevision | VerifyAdoption, Field(discriminator="kind")]
