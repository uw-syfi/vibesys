"""Operations the single strategy declares; execution belongs to the owning libraries.

The request and outcome shapes are the shared bases of `vs_core.api`; this module pins single's
namespace (the persisted kind names) and its prompt-context and decoded-reading types. Capture
is declared for the profile-guided preset and ignored by the plain one.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel

from vibesys.metrics import EvidenceReading
from vibesys.orchestration.single.strategy._prompts import PromptContext
from vs_core.api import (
    CaptureCommandBase,
    CommandCapture,
    EvidenceReadingsBase,
    InterpretEvidenceBase,
    OperationRegistration,
    OperationRegistry,
    ParentVerification,
    RenderedArtifacts,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    RetentionReceipt,
    StrategyOperations,
    VerifyParentRevisionBase,
)

RENDER_KIND = "single.render_role_artifacts"
VERIFY_KIND = "single.verify_parent_revision"
INTERPRET_KIND = "single.interpret_evidence"
RETAIN_KIND = "single.retain_verified_revision"
CAPTURE_KIND = "single.capture_command"

__all__ = [
    "CAPTURE_KIND",
    "INTERPRET_KIND",
    "OPERATIONS",
    "RENDER_KIND",
    "RETAIN_KIND",
    "VERIFY_KIND",
    "CaptureCommand",
    "CommandCapture",
    "EvidenceReadings",
    "InterpretEvidence",
    "ParentVerification",
    "RenderRoleArtifacts",
    "RenderedArtifacts",
    "RetainVerifiedRevision",
    "RetentionReceipt",
    "VerifyParentRevision",
    "single_operation_registrations",
    "single_operation_registry",
]


class RenderRoleArtifacts(RenderRoleArtifactsBase[PromptContext]):
    """Render one role prompt from a template and store it as artifacts."""

    kind: Literal["single.render_role_artifacts"] = "single.render_role_artifacts"


class VerifyParentRevision(VerifyParentRevisionBase):
    """Verify that a parent revision exports to its digest (declared; single builds on core's own)."""

    kind: Literal["single.verify_parent_revision"] = "single.verify_parent_revision"


class EvidenceReadings(EvidenceReadingsBase[EvidenceReading]):
    """Decoded readings, one per requested evidence ID the owner could decode."""


class InterpretEvidence(InterpretEvidenceBase):
    """Decode accepted evidence into typed metrics; the strategy never parses artifacts."""

    kind: Literal["single.interpret_evidence"] = "single.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = EvidenceReadings


class RetainVerifiedRevision(RetainVerifiedRevisionBase):
    """Retain an accuracy-verified exact revision while its producer may still edit."""

    kind: Literal["single.retain_verified_revision"] = "single.retain_verified_revision"


class CaptureCommand(CaptureCommandBase):
    """Run the profile-guided attribution command at a retained revision."""

    kind: Literal["single.capture_command"] = "single.capture_command"


OPERATIONS = StrategyOperations(
    namespace="single",
    render=RenderRoleArtifacts,
    verify=VerifyParentRevision,
    interpret=InterpretEvidence,
    retain=RetainVerifiedRevision,
    capture=CaptureCommand,
)


def single_operation_registrations() -> tuple[OperationRegistration, ...]:
    """Codec registrations the launch assembles beside other libraries' operations."""
    return OPERATIONS.registrations()


def single_operation_registry() -> OperationRegistry:
    """A closed codec holding only the single strategy's operations."""
    return OPERATIONS.registry()
