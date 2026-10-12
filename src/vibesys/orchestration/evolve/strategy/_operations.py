"""Operations the evolve strategy declares; execution belongs to the owning libraries.

The request and outcome shapes are the shared bases of `vs_core.api`; this module pins evolve's
namespace (the persisted kind names) and its prompt-context and decoded-reading types.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel

from vibesys.metrics import EvidenceReading
from vibesys.orchestration.evolve.strategy._prompts import PromptContext
from vs_core.api import (
    EvidenceReadingsBase,
    ExportPatchBase,
    InterpretEvidenceBase,
    OperationRegistration,
    OperationRegistry,
    ParentVerification,
    PatchExport,
    RenderedArtifacts,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    RetentionReceipt,
    StrategyOperations,
    VerifyParentRevisionBase,
)

RENDER_KIND = "evolve.render_role_artifacts"
VERIFY_KIND = "evolve.verify_parent_revision"
INTERPRET_KIND = "evolve.interpret_evidence"
RETAIN_KIND = "evolve.retain_verified_revision"
EXPORT_PATCH_KIND = "evolve.export_patch"

__all__ = [
    "EXPORT_PATCH_KIND",
    "INTERPRET_KIND",
    "OPERATIONS",
    "RENDER_KIND",
    "RETAIN_KIND",
    "VERIFY_KIND",
    "EvidenceReadings",
    "ExportPatch",
    "InterpretEvidence",
    "ParentVerification",
    "PatchExport",
    "RenderRoleArtifacts",
    "RenderedArtifacts",
    "RetainVerifiedRevision",
    "RetentionReceipt",
    "VerifyParentRevision",
    "evolve_operation_registrations",
    "evolve_operation_registry",
]


class RenderRoleArtifacts(RenderRoleArtifactsBase[PromptContext]):
    """Render one role prompt from a template and store it as artifacts."""

    kind: Literal["evolve.render_role_artifacts"] = "evolve.render_role_artifacts"


class VerifyParentRevision(VerifyParentRevisionBase):
    """Verify that a parent revision exports to its digest (declared; evolve builds on core's own)."""

    kind: Literal["evolve.verify_parent_revision"] = "evolve.verify_parent_revision"


class EvidenceReadings(EvidenceReadingsBase[EvidenceReading]):
    """Decoded readings, one per requested evidence ID the owner could decode."""


class InterpretEvidence(InterpretEvidenceBase):
    """Decode accepted evidence into typed metrics; the strategy never parses artifacts."""

    kind: Literal["evolve.interpret_evidence"] = "evolve.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = EvidenceReadings


class RetainVerifiedRevision(RetainVerifiedRevisionBase):
    """Retain an accuracy-verified exact revision while its producer may still edit."""

    kind: Literal["evolve.retain_verified_revision"] = "evolve.retain_verified_revision"


class ExportPatch(ExportPatchBase):
    """Export a passing candidate's patch for a selector that ranks candidates by their code."""

    kind: Literal["evolve.export_patch"] = "evolve.export_patch"


OPERATIONS = StrategyOperations(
    namespace="evolve",
    render=RenderRoleArtifacts,
    verify=VerifyParentRevision,
    interpret=InterpretEvidence,
    retain=RetainVerifiedRevision,
    export_patch=ExportPatch,
)


def evolve_operation_registrations() -> tuple[OperationRegistration, ...]:
    """Codec registrations the launch assembles beside other libraries' operations."""
    return OPERATIONS.registrations()


def evolve_operation_registry() -> OperationRegistry:
    """A closed codec holding only the evolve strategy's operations."""
    return OPERATIONS.registry()
