"""The four operations every strategy on the core lifecycle declares, and the kit that registers them.

Core registers only a codec for each operation: a request model, an outcome model and a
descriptor. The runtime performs each by *role* (`vs_runtime.api.core.OperationRole`, mapped by `operation_roles`), independent of the strategy's
own kind names, so the request and outcome shapes, the descriptors and the owners are the same
code for every strategy. A strategy supplies only what is its own:

- its namespace, which prefixes its persisted kind names (`dynamic.render_role_artifacts`), so
  two strategies never collide in one registry;
- its prompt-context type, which only its renderer templates understand;
- its decoded-reading type, which is policy above the runtime.

The shared bases here carry every field and rule; a strategy subclasses each to pin its kind
(a `Literal`, because the codec discriminates on it) and its types, then hands the four
subclasses to `StrategyOperations`, which checks the kinds against the namespace and builds
registrations, the registry, the role mapping and the schema references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from vs_core._registry import OperationRegistration, OperationRegistry
from vs_core.types.common import (
    ArtifactRef,
    LifecycleClass,
    OperationDescriptor,
    OperationSchemaRef,
    RevisionAuthority,
    RevisionRef,
    SchemaRef,
    Value,
)
from vs_core.types.evaluation import EvidenceRef
from vs_core.types.strategy import OperationRequest

type Status = Literal["succeeded", "pending", "cancelled", "rejected", "failed", "unknown"]

RENDER_SUFFIX = "render_role_artifacts"
VERIFY_SUFFIX = "verify_parent_revision"
INTERPRET_SUFFIX = "interpret_evidence"
RETAIN_SUFFIX = "retain_verified_revision"
CAPTURE_SUFFIX = "capture_command"
VALIDATE_LOCAL_SUFFIX = "validate_local"


class RenderedArtifacts(Value):
    """Prompt artifacts stored by the renderer, keyed by the request's identity."""

    status: Status
    prompts: tuple[ArtifactRef, ...] = ()
    tool_policy: ArtifactRef | None = None


class RenderRoleArtifactsBase[C: Value](OperationRequest):
    """Render one role prompt from a template and store it as artifacts.

    Idempotent per (subject, ordinal): the executor must return the same artifacts
    for a repeated request, so replay after a crash never forks a prompt. ``C`` is the
    strategy's prompt-context type.
    """

    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RenderedArtifacts
    subject: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    context: C


class ParentVerification(Value):
    """Whether an offered parent still reproduces its retained, evaluated content."""

    status: Status
    verified: bool
    detail: str = ""


class VerifyParentRevisionBase(OperationRequest):
    """Verify, before any child starts, that a parent revision exports to its digest."""

    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = ParentVerification
    parent: RevisionRef


class EvidenceReadingsBase[R: Value](Value):
    """Decoded readings, one per requested evidence ID the owner could decode.

    ``R`` is the strategy's reading type.
    """

    status: Status
    readings: tuple[R, ...] = ()


class InterpretEvidenceBase(OperationRequest):
    """Decode accepted evidence into typed metrics; the strategy never parses artifacts.

    Carries each core `EvidenceRef` by value, copied from the run's evidence ledger,
    so the owner reads exactly the records the strategy names (full `EvidenceKey`,
    candidate, scope and provenance) and never searches by bare evidence ID. A strategy
    subclass sets ``outcome_model`` to its concrete readings model.
    """

    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)


class RetentionReceipt(Value):
    """Proof that an independently captured immutable revision is retained."""

    status: Status
    retained: bool
    detail: str = ""


class RetainVerifiedRevisionBase(OperationRequest):
    """Retain an accuracy-verified exact revision while its producer may still edit.

    Proves the revision was captured independently of the active writer and that
    the accuracy proof, a core `EvidenceRef` carried by value, names exactly it. It
    never restores, snapshots or mutates the producer's workspace.
    """

    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RetentionReceipt
    revision: RevisionRef
    accuracy_proof: EvidenceRef


class CommandCapture(Value):
    """What a captured command printed and how it ended."""

    status: Status
    exit_code: int | None = None
    output: str = ""
    truncated: bool = False
    detail: str = ""


class CaptureCommandBase(OperationRequest):
    """Run a configured argv in an isolated workspace holding a retained revision.

    The runtime appends ``output_argument`` and a temporary path, reads the file the command
    wrote, bounds the output and removes the file; the strategy owns only the choice of command
    and the protocol of its output. The workspace is discarded after the run, so the operation
    changes nothing durable.
    """

    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = CommandCapture
    revision: RevisionRef
    argv: tuple[str, ...] = Field(min_length=1)
    output_argument: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class LocalValidationReport(Value):
    """How the candidate-authored validation recipes ended, with the durable report.

    ``recipe``, ``error`` and ``output`` describe the first recipe that failed (empty when all
    passed): data for the strategy's feedback template, never prompt text. ``recipe_unusable``
    marks an input error in the agent's own reply (the recipe file is missing or malformed), not
    a failure of the candidate. ``report`` names the stored JSON report of every recipe's result.
    """

    status: Status
    passed: bool = False
    recipe_unusable: bool = False
    recipe: str = ""
    error: str = ""
    output: str = ""
    report: ArtifactRef | None = None
    detail: str = ""


class ValidateLocalBase(OperationRequest):
    """Run an agent-authored validation recipe file against a retained revision.

    The recipes are trusted commands the runtime executes in a throwaway isolated workspace
    holding exactly ``revision``; a recipe that changes that workspace fails. The workspace is
    discarded afterwards and the JSON report of the results is stored as an artifact, so the
    operation changes nothing in the run's workspaces and a replay may run it again.
    """

    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = LocalValidationReport
    revision: RevisionRef
    recipe_artifact: str = Field(min_length=1)
    """Workspace-relative path of the recipe file in the revision, as the agent reported it."""


class OperationKindError(ValueError):
    """A strategy's operation does not carry the kind its namespace prescribes."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"operation kind {actual!r} must be {expected!r}")


@dataclass(frozen=True)
class StrategyOperations:
    """The operations of one strategy namespace, by the role the runtime performs for each."""

    namespace: str
    render: type[OperationRequest]
    """A subclass of `RenderRoleArtifactsBase` for the strategy's context type."""
    verify: type[VerifyParentRevisionBase]
    interpret: type[InterpretEvidenceBase]
    retain: type[RetainVerifiedRevisionBase]
    capture: type[CaptureCommandBase] | None = None
    """The optional command capture a profile-guided strategy declares."""
    validate_local: type[ValidateLocalBase] | None = None
    """The optional validation of an agent-authored recipe file a reviewing strategy declares."""

    def __post_init__(self) -> None:
        for suffix, model in self._by_suffix():
            expected = f"{self.namespace}.{suffix}"
            actual = model.model_fields["kind"].default
            if actual != expected:
                raise OperationKindError(expected, str(actual))

    def _by_suffix(self) -> tuple[tuple[str, type[OperationRequest]], ...]:
        required = (
            (RENDER_SUFFIX, self.render),
            (VERIFY_SUFFIX, self.verify),
            (INTERPRET_SUFFIX, self.interpret),
            (RETAIN_SUFFIX, self.retain),
        )
        capture = ((CAPTURE_SUFFIX, self.capture),) if self.capture is not None else ()
        validate = (
            ((VALIDATE_LOCAL_SUFFIX, self.validate_local),)
            if self.validate_local is not None
            else ()
        )
        return (*required, *capture, *validate)

    def registration_of(self, suffix: str) -> OperationRegistration | None:
        """The registration of the operation with this suffix, or None when not declared."""
        for registration in self.registrations():
            if registration.descriptor.kind == f"{self.namespace}.{suffix}":
                return registration
        return None

    def registrations(self) -> tuple[OperationRegistration, ...]:
        """Codec registrations the launch assembles beside other libraries' operations."""
        return tuple(
            self._registration(
                suffix,
                model,
                revision_authority=(
                    RevisionAuthority.RETAIN if suffix == RETAIN_SUFFIX else RevisionAuthority.NONE
                ),
            )
            for suffix, model in self._by_suffix()
        )

    def registry(self) -> OperationRegistry:
        """A closed codec holding only this strategy's operations."""
        return OperationRegistry(self.registrations())

    def schema_ref(self, kind: str) -> OperationSchemaRef:
        """The declared schema reference of one operation kind."""
        for registration in self.registrations():
            descriptor = registration.descriptor
            if descriptor.kind == kind:
                return OperationSchemaRef(
                    kind=kind,
                    request_schema=descriptor.request_schema,
                    outcome_schema=descriptor.outcome_schema,
                    lifecycle=descriptor.lifecycle,
                )
        raise KeyError(kind)

    def _registration(
        self,
        suffix: str,
        request: type[OperationRequest],
        *,
        revision_authority: RevisionAuthority = RevisionAuthority.NONE,
    ) -> OperationRegistration:
        stem = suffix.replace("_", "-")
        lifecycle = request.model_fields["lifecycle"].default
        return OperationRegistration(
            descriptor=OperationDescriptor(
                kind=f"{self.namespace}.{suffix}",
                request_schema=SchemaRef(name=f"{self.namespace}.{stem}", version=1),
                outcome_schema=SchemaRef(name=f"{self.namespace}.{stem}-outcome", version=1),
                lifecycle=lifecycle,
                inspect=lifecycle is not LifecycleClass.QUERY,
                revision_authority=revision_authority,
            ),
            request_model=request,
            outcome_model=request.outcome_model,
        )
