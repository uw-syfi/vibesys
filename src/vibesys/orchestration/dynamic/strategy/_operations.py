"""Operations the dynamic strategy declares; execution belongs to the owning libraries.

Core registers only a codec for each operation. The request and outcome shapes are the shared
bases of `vs_core.api` (`vs_core.api.StrategyOperations`); this module pins dynamic's namespace
(the persisted kind names), its prompt-context type and its decoded-reading type. A runtime
executor performs each one and reports an outcome the strategy reads back as `OperationResult`.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel

from vibesys.metrics import EvidenceReading
from vs_core.api import (
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

from ._prompts import PromptContext

RENDER_KIND = "dynamic.render_role_artifacts"
VERIFY_KIND = "dynamic.verify_parent_revision"
INTERPRET_KIND = "dynamic.interpret_evidence"
RETAIN_KIND = "dynamic.retain_verified_revision"

__all__ = [
    "INTERPRET_KIND",
    "OPERATIONS",
    "RENDER_KIND",
    "RETAIN_KIND",
    "VERIFY_KIND",
    "EvidenceReadings",
    "InterpretEvidence",
    "ParentVerification",
    "RenderRoleArtifacts",
    "RenderedArtifacts",
    "RetainVerifiedRevision",
    "RetentionReceipt",
    "VerifyParentRevision",
    "dynamic_operation_registrations",
    "dynamic_operation_registry",
]


class RenderRoleArtifacts(RenderRoleArtifactsBase[PromptContext]):
    """Render one role prompt from a template and store it as artifacts."""

    kind: Literal["dynamic.render_role_artifacts"] = "dynamic.render_role_artifacts"


class VerifyParentRevision(VerifyParentRevisionBase):
    """Verify, before any child starts, that a parent revision exports to its digest."""

    kind: Literal["dynamic.verify_parent_revision"] = "dynamic.verify_parent_revision"


class EvidenceReadings(EvidenceReadingsBase[EvidenceReading]):
    """Decoded readings, one per requested evidence ID the owner could decode."""


class InterpretEvidence(InterpretEvidenceBase):
    """Decode accepted evidence into typed metrics; the strategy never parses artifacts."""

    kind: Literal["dynamic.interpret_evidence"] = "dynamic.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = EvidenceReadings


class RetainVerifiedRevision(RetainVerifiedRevisionBase):
    """Retain an accuracy-verified exact revision while its producer may still edit."""

    kind: Literal["dynamic.retain_verified_revision"] = "dynamic.retain_verified_revision"


OPERATIONS = StrategyOperations(
    namespace="dynamic",
    render=RenderRoleArtifacts,
    verify=VerifyParentRevision,
    interpret=InterpretEvidence,
    retain=RetainVerifiedRevision,
)


def dynamic_operation_registrations() -> tuple[OperationRegistration, ...]:
    """Codec registrations the launch assembles beside other libraries' operations."""
    return OPERATIONS.registrations()


def dynamic_operation_registry() -> OperationRegistry:
    """A closed codec holding only the dynamic strategy's operations."""
    return OPERATIONS.registry()
