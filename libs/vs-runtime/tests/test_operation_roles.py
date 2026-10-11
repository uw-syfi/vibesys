"""The runtime performs a strategy's operations by role, whatever the strategy names them."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from vs_core.api import (
    EvidenceReadingsBase,
    InterpretEvidenceBase,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    StrategyOperations,
    Value,
    VerifyParentRevisionBase,
)
from vs_runtime.api.core import OperationRole, operation_roles

if TYPE_CHECKING:
    from pydantic import BaseModel


class Context(Value):
    text: str = ""


class Readings(EvidenceReadingsBase[Context]):
    pass


class Render(RenderRoleArtifactsBase[Context]):
    kind: Literal["demo.render_role_artifacts"] = "demo.render_role_artifacts"


class Verify(VerifyParentRevisionBase):
    kind: Literal["demo.verify_parent_revision"] = "demo.verify_parent_revision"


class Interpret(InterpretEvidenceBase):
    kind: Literal["demo.interpret_evidence"] = "demo.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = Readings


class Retain(RetainVerifiedRevisionBase):
    kind: Literal["demo.retain_verified_revision"] = "demo.retain_verified_revision"


def test_every_runtime_role_gets_the_operation_of_its_name() -> None:
    roles = operation_roles(StrategyOperations("demo", Render, Verify, Interpret, Retain))
    assert {role: item.descriptor.kind for role, item in roles.items()} == {
        OperationRole.RENDER_ARTIFACTS: "demo.render_role_artifacts",
        OperationRole.VERIFY_REVISION: "demo.verify_parent_revision",
        OperationRole.INTERPRET_EVIDENCE: "demo.interpret_evidence",
        OperationRole.RETAIN_REVISION: "demo.retain_verified_revision",
    }
