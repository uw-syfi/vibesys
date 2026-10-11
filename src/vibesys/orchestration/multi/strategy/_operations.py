"""Operations the multi strategy declares; execution belongs to the owning libraries.

The request and outcome shapes are the shared bases of `vs_core.api`; this module pins multi's
namespace (the persisted kind names) and its prompt-context and decoded-reading types. Capture
is declared for the profile-guided preset and validation for the reviewed round; each is
optional, because a run without isolated candidate workspaces cannot offer them.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel

from vibesys.metrics import EvidenceReading
from vibesys.orchestration.multi.strategy._prompts import PromptContext
from vs_core.api import (
    CaptureCommandBase,
    CommandCapture,
    EvidenceReadingsBase,
    InterpretEvidenceBase,
    LocalValidationReport,
    OperationRegistration,
    OperationRegistry,
    ParentVerification,
    RenderedArtifacts,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    RetentionReceipt,
    StrategyOperations,
    ValidateLocalBase,
    VerifyParentRevisionBase,
)

RENDER_KIND = "multi.render_role_artifacts"
VERIFY_KIND = "multi.verify_parent_revision"
INTERPRET_KIND = "multi.interpret_evidence"
RETAIN_KIND = "multi.retain_verified_revision"
CAPTURE_KIND = "multi.capture_command"
VALIDATE_LOCAL_KIND = "multi.validate_local"

__all__ = [
    "CAPTURE_KIND",
    "INTERPRET_KIND",
    "OPERATIONS",
    "RENDER_KIND",
    "RETAIN_KIND",
    "VALIDATE_LOCAL_KIND",
    "VERIFY_KIND",
    "CaptureCommand",
    "CommandCapture",
    "EvidenceReadings",
    "InterpretEvidence",
    "LocalValidationReport",
    "ParentVerification",
    "RenderRoleArtifacts",
    "RenderedArtifacts",
    "RetainVerifiedRevision",
    "RetentionReceipt",
    "ValidateLocal",
    "VerifyParentRevision",
    "multi_operation_registrations",
    "multi_operation_registry",
]


class RenderRoleArtifacts(RenderRoleArtifactsBase[PromptContext]):
    """Render one role prompt from a template and store it as artifacts."""

    kind: Literal["multi.render_role_artifacts"] = "multi.render_role_artifacts"


class VerifyParentRevision(VerifyParentRevisionBase):
    """Verify that a parent revision exports to its digest (declared; multi builds on core's own)."""

    kind: Literal["multi.verify_parent_revision"] = "multi.verify_parent_revision"


class EvidenceReadings(EvidenceReadingsBase[EvidenceReading]):
    """Decoded readings, one per requested evidence ID the owner could decode."""


class InterpretEvidence(InterpretEvidenceBase):
    """Decode accepted evidence into typed metrics; the strategy never parses artifacts."""

    kind: Literal["multi.interpret_evidence"] = "multi.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = EvidenceReadings


class RetainVerifiedRevision(RetainVerifiedRevisionBase):
    """Retain an accuracy-verified exact revision while its producer may still edit."""

    kind: Literal["multi.retain_verified_revision"] = "multi.retain_verified_revision"


class CaptureCommand(CaptureCommandBase):
    """Run the profile-guided attribution command at a retained revision."""

    kind: Literal["multi.capture_command"] = "multi.capture_command"


class ValidateLocal(ValidateLocalBase):
    """Run the validation recipes the implementer wrote against its retained candidate."""

    kind: Literal["multi.validate_local"] = "multi.validate_local"


OPERATIONS = StrategyOperations(
    namespace="multi",
    render=RenderRoleArtifacts,
    verify=VerifyParentRevision,
    interpret=InterpretEvidence,
    retain=RetainVerifiedRevision,
    capture=CaptureCommand,
    validate_local=ValidateLocal,
)


def multi_operation_registrations() -> tuple[OperationRegistration, ...]:
    """Codec registrations the launch assembles beside other libraries' operations."""
    return OPERATIONS.registrations()


def multi_operation_registry() -> OperationRegistry:
    """A closed codec holding only the multi strategy's operations."""
    return OPERATIONS.registry()
