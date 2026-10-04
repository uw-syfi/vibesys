"""Assessment finality, retention eligibility and separate adoption."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .common import (
    AssessmentKind,
    AttemptRef,
    CompletionStatus,
    DecisionId,
    EvidenceId,
    EvidenceKind,
    InvocationRef,
    Observation,
    RequestBase,
    RevisionRef,
    RoleId,
    SchemaRef,
    SettlementId,
    Value,
)


class AssessmentProposal(Value):
    """Assessment proposal lifecycle contract."""

    kind: AssessmentKind
    verdict: Literal["satisfied", "rejected", "deferred"]
    sources: tuple[InvocationRef | EvidenceId, ...]
    candidate: RevisionRef | None
    schema_version: int = Field(ge=1)


class EvidenceRequirement(Value):
    """Exact required kind, with explicit measurement provenance and purpose."""

    kind: EvidenceKind
    provenance: Literal["trusted", "self-report"]
    purpose: Literal["baseline", "local-validation", "official", "profile"] | None = None


class AssessmentAuthority(Value):
    """Declared role and output-schema authority for one assessment kind.

    Invocation sources require exact accepted final schema-valid output and
    checkpoint or read-only revision attribution. No role has implicit authority.
    """

    kind: AssessmentKind
    role_id: RoleId
    output_schema: SchemaRef


class EvidenceRequirements(Value):
    """Eligibility proof requirements, independent of scientific ranking.

    Required final successful evidence matches exact scope/generation, recorded
    job, candidate ID and digest, fingerprints, provenance, purpose and kind.
    Every required assessment is satisfied. Empty authorities allow evidence
    sources but grant no invocation role authority. WIP alone never qualifies.
    """

    assessment_authorities: tuple[AssessmentAuthority, ...] = ()
    required_evidence: tuple[EvidenceRequirement, ...] = ()
    required_assessments: tuple[AssessmentKind, ...] = ()
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


class SettlementDependencyResolved(Value):
    """Wake pending settlements after semantic prerequisite completion.

    This is a notification, not acceptance proof. Settlement A rechecks the
    exact accepted decision receipt and all prerequisites against its context;
    duplicate notifications cannot create a second settlement.
    """

    kind: Literal["settlement_dependency_resolved"] = "settlement_dependency_resolved"
    decision_id: DecisionId
    status: CompletionStatus


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
    AssessmentSubmitted
    | SettlementDependencyResolved
    | OwnershipSettled
    | WinnerProposed
    | AdoptionObserved
    | AttemptSettled,
    Field(discriminator="kind"),
]
type AdoptionRequest = Annotated[AdoptRevision | VerifyAdoption, Field(discriminator="kind")]
