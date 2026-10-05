"""Operations the dynamic strategy declares; execution belongs to the owning libraries.

Core registers only a codec for each operation. The request and outcome models
are pure values; a runtime executor performs each one and reports an outcome the
strategy reads back as `OperationResult`. All four are declared here until the
owning libraries (workspaces, prompts, evaluation) publish them.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from vs_core.api import (
    ArtifactRef,
    EvidenceRef,
    LifecycleClass,
    OperationDescriptor,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    OperationSchemaRef,
    RevisionAuthority,
    RevisionRef,
    SchemaRef,
    Value,
)

from ._prompts import PromptContext
from ._rows import EvidenceReading

type Status = Literal["succeeded", "pending", "cancelled", "rejected", "failed", "unknown"]

RENDER_KIND = "dynamic.render_role_artifacts"
VERIFY_KIND = "dynamic.verify_parent_revision"
INTERPRET_KIND = "dynamic.interpret_evidence"
RETAIN_KIND = "dynamic.retain_verified_revision"


class RenderedArtifacts(Value):
    """Prompt artifacts stored by the renderer, keyed by the request's identity."""

    status: Status
    prompts: tuple[ArtifactRef, ...] = ()
    tool_policy: ArtifactRef | None = None


class RenderRoleArtifacts(OperationRequest):
    """Render one role prompt from a template and store it as artifacts.

    Idempotent per (subject, ordinal): the executor must return the same artifacts
    for a repeated request, so replay after a crash never forks a prompt.
    """

    kind: Literal["dynamic.render_role_artifacts"] = "dynamic.render_role_artifacts"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RenderedArtifacts
    subject: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    context: PromptContext


class ParentVerification(Value):
    """Whether an offered parent still reproduces its retained, evaluated content."""

    status: Status
    verified: bool
    detail: str = ""


class VerifyParentRevision(OperationRequest):
    """Verify, before any child starts, that a parent revision exports to its digest."""

    kind: Literal["dynamic.verify_parent_revision"] = "dynamic.verify_parent_revision"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = ParentVerification
    parent: RevisionRef


class EvidenceReadings(Value):
    """Decoded readings, one per requested evidence ID the owner could decode."""

    status: Status
    readings: tuple[EvidenceReading, ...] = ()


class InterpretEvidence(OperationRequest):
    """Decode accepted evidence into typed metrics; the strategy never parses artifacts.

    Carries each core `EvidenceRef` by value, copied from the run's evidence ledger,
    so the owner reads exactly the records the strategy names (full `EvidenceKey`,
    candidate, scope and provenance) and never searches by bare evidence ID.
    """

    kind: Literal["dynamic.interpret_evidence"] = "dynamic.interpret_evidence"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = EvidenceReadings
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)


class RetentionReceipt(Value):
    """Proof that an independently captured immutable revision is retained."""

    status: Status
    retained: bool
    detail: str = ""


class RetainVerifiedRevision(OperationRequest):
    """Retain an accuracy-verified exact revision while its producer may still edit.

    Proves the revision was captured independently of the active writer and that
    the accuracy proof, a core `EvidenceRef` carried by value, names exactly it. It
    never restores, snapshots or mutates the producer's workspace.
    """

    kind: Literal["dynamic.retain_verified_revision"] = "dynamic.retain_verified_revision"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RetentionReceipt
    revision: RevisionRef
    accuracy_proof: EvidenceRef


def _registration(
    kind: str,
    request: type[OperationRequest],
    outcome: type[BaseModel],
    lifecycle: LifecycleClass,
    *,
    revision_authority: RevisionAuthority = RevisionAuthority.NONE,
) -> OperationRegistration:
    stem = kind.removeprefix("dynamic.").replace("_", "-")
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind=kind,
            request_schema=SchemaRef(name=f"dynamic.{stem}", version=1),
            outcome_schema=SchemaRef(name=f"dynamic.{stem}-outcome", version=1),
            lifecycle=lifecycle,
            inspect=lifecycle is not LifecycleClass.QUERY,
            revision_authority=revision_authority,
        ),
        request_model=request,
        outcome_model=outcome,
    )


def dynamic_operation_registrations() -> tuple[OperationRegistration, ...]:
    """Codec registrations the launch assembles beside other libraries' operations."""
    return (
        _registration(
            RENDER_KIND, RenderRoleArtifacts, RenderedArtifacts, LifecycleClass.IDEMPOTENT_WRITE
        ),
        _registration(VERIFY_KIND, VerifyParentRevision, ParentVerification, LifecycleClass.QUERY),
        _registration(INTERPRET_KIND, InterpretEvidence, EvidenceReadings, LifecycleClass.QUERY),
        _registration(
            RETAIN_KIND,
            RetainVerifiedRevision,
            RetentionReceipt,
            LifecycleClass.IDEMPOTENT_WRITE,
            revision_authority=RevisionAuthority.RETAIN,
        ),
    )


def dynamic_operation_registry() -> OperationRegistry:
    """A closed codec holding only the dynamic strategy's operations."""
    return OperationRegistry(dynamic_operation_registrations())


def schema_ref(kind: str) -> OperationSchemaRef:
    """The declared schema reference of one dynamic operation kind."""
    for registration in dynamic_operation_registrations():
        descriptor = registration.descriptor
        if descriptor.kind == kind:
            return OperationSchemaRef(
                kind=kind,
                request_schema=descriptor.request_schema,
                outcome_schema=descriptor.outcome_schema,
                lifecycle=descriptor.lifecycle,
            )
    message = f"unknown dynamic operation kind {kind!r}"
    raise KeyError(message)
