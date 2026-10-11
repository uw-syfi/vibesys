"""Operations the issue-queue strategy declares; execution belongs to the owning libraries.

The strategy renders prompts through the shared operation kit under the `plain` namespace
(`vs_core.api.StrategyOperations`) and reads and changes the issue board through the
operations `vs_issue_tracker` owns. Core registers only a codec for each; a runtime owner
performs them and reports an outcome the strategy reads back as `OperationResult`.

The verify, interpret and retain operations are declared because the kit registers all four
together; this strategy requires only the render operation, since it builds on revisions it
retained itself and measures nothing of its own.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel

from vibesys.metrics import EvidenceReading
from vibesys.orchestration.issue_queue.strategy._prompts import PromptContext
from vs_core.api import (
    EvidenceReadingsBase,
    InterpretEvidenceBase,
    OperationRegistration,
    OperationRegistry,
    OperationSchemaRef,
    RenderedArtifacts,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    StrategyOperations,
    VerifyParentRevisionBase,
)
from vs_issue_tracker.api import (
    APPLY_KIND,
    LOG_KIND,
    READ_KIND,
    AppliedChanges,
    ApplyIssueChanges,
    BoardSnapshot,
    LoggedProgress,
    LogProgress,
    ReadBoard,
    board_operation_registrations,
)

RENDER_KIND = "plain.render_role_artifacts"
VERIFY_KIND = "plain.verify_parent_revision"
INTERPRET_KIND = "plain.interpret_evidence"
RETAIN_KIND = "plain.retain_verified_revision"


class RenderRoleArtifacts(RenderRoleArtifactsBase[PromptContext]):
    """Render one role prompt from a template and store it as artifacts."""

    kind: Literal["plain.render_role_artifacts"] = "plain.render_role_artifacts"


class VerifyParentRevision(VerifyParentRevisionBase):
    """Declared with the kit; the issue queue builds only on revisions it retained itself."""

    kind: Literal["plain.verify_parent_revision"] = "plain.verify_parent_revision"


class EvidenceReadings(EvidenceReadingsBase[EvidenceReading]):
    """Decoded readings, one per requested evidence ID the owner could decode."""


class InterpretEvidence(InterpretEvidenceBase):
    """Declared with the kit; the issue queue reads no evidence."""

    kind: Literal["plain.interpret_evidence"] = "plain.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = EvidenceReadings


class RetainVerifiedRevision(RetainVerifiedRevisionBase):
    """Declared with the kit; the issue queue retains through attempt settlement."""

    kind: Literal["plain.retain_verified_revision"] = "plain.retain_verified_revision"


OPERATIONS = StrategyOperations(
    namespace="plain",
    render=RenderRoleArtifacts,
    verify=VerifyParentRevision,
    interpret=InterpretEvidence,
    retain=RetainVerifiedRevision,
)


def plain_operation_registrations() -> tuple[OperationRegistration, ...]:
    """Every operation the strategy's runs record, in the order its declaration lists them.

    The required ones come first (render, then the board's three), then the optional ones the
    kit registers beside render.
    """
    render, *unused = OPERATIONS.registrations()
    return (render, *board_operation_registrations(), *unused)


def board_schema_ref(kind: str) -> OperationSchemaRef:
    """The declared schema reference of one board operation kind."""
    for registration in board_operation_registrations():
        descriptor = registration.descriptor
        if descriptor.kind == kind:
            return OperationSchemaRef(
                kind=kind,
                request_schema=descriptor.request_schema,
                outcome_schema=descriptor.outcome_schema,
                lifecycle=descriptor.lifecycle,
            )
    raise KeyError(kind)


def plain_operation_registry() -> OperationRegistry:
    """A closed codec holding only the issue-queue strategy's operations."""
    return OperationRegistry(plain_operation_registrations())


__all__ = [
    "APPLY_KIND",
    "INTERPRET_KIND",
    "LOG_KIND",
    "OPERATIONS",
    "READ_KIND",
    "RENDER_KIND",
    "RETAIN_KIND",
    "VERIFY_KIND",
    "AppliedChanges",
    "ApplyIssueChanges",
    "BoardSnapshot",
    "EvidenceReadings",
    "InterpretEvidence",
    "LogProgress",
    "LoggedProgress",
    "ReadBoard",
    "RenderRoleArtifacts",
    "RenderedArtifacts",
    "RetainVerifiedRevision",
    "VerifyParentRevision",
    "board_schema_ref",
    "plain_operation_registrations",
    "plain_operation_registry",
]
