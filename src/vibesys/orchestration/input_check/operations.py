"""Runtime-owned operations used to decode the input gate evidence."""

from typing import ClassVar, Literal

from pydantic import BaseModel

from vibesys.metrics import EvidenceReading
from vs_core.api.search import (
    EvidenceReadingsBase,
    InterpretEvidenceBase,
    RenderRoleArtifactsBase,
    StrategyOperations,
    Value,
    VerifyParentRevisionBase,
)


class InputCheckPromptContext(Value):
    """Empty because the input check never renders an agent prompt."""


class RenderRoleArtifacts(RenderRoleArtifactsBase[InputCheckPromptContext]):
    """Required operation shape; a zero-agent run never requests it."""

    kind: Literal["check-input.render_role_artifacts"] = "check-input.render_role_artifacts"


class VerifyParentRevision(VerifyParentRevisionBase):
    """Required operation shape; the input check has no candidate parent."""

    kind: Literal["check-input.verify_parent_revision"] = "check-input.verify_parent_revision"


class EvidenceReadings(EvidenceReadingsBase[EvidenceReading]):
    """Decoded readings from the task's configured gate evidence."""


class InterpretEvidence(InterpretEvidenceBase):
    """Ask the evidence owner to decode the input's gate evidence."""

    kind: Literal["check-input.interpret_evidence"] = "check-input.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = EvidenceReadings


OPERATIONS = StrategyOperations(
    namespace="check-input",
    render=RenderRoleArtifacts,
    verify=VerifyParentRevision,
    interpret=InterpretEvidence,
)


__all__ = [
    "OPERATIONS",
    "EvidenceReadings",
    "InputCheckPromptContext",
    "InterpretEvidence",
    "RenderRoleArtifacts",
    "VerifyParentRevision",
]
