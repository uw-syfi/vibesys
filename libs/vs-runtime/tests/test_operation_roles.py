"""The runtime performs a strategy's operations by role, whatever the strategy names them."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from vs_core.api import (
    CaptureCommandBase,
    EvidenceReadingsBase,
    InterpretEvidenceBase,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    StrategyOperations,
    Value,
    VerifyParentRevisionBase,
)
from vs_runtime.api.core import (
    OperationRole,
    RefusalReason,
    build_operation_catalog,
    operation_roles,
)

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


class Capture(CaptureCommandBase):
    kind: Literal["demo.capture_command"] = "demo.capture_command"


def test_an_optional_capture_maps_to_its_own_role_and_is_refused_without_a_runner() -> None:
    operations = StrategyOperations("demo", Render, Verify, Interpret, Retain, capture=Capture)
    roles = operation_roles(operations)
    assert list(roles) == list(OperationRole)
    catalog = build_operation_catalog(
        {OperationRole.CAPTURE_COMMAND: roles[OperationRole.CAPTURE_COMMAND]}, {}
    )
    (entry,) = catalog.entries
    assert entry.owner is None
    assert entry.refusal is RefusalReason.NO_COMMAND_RUNNER


def test_every_runtime_role_gets_the_operation_of_its_name() -> None:
    roles = operation_roles(StrategyOperations("demo", Render, Verify, Interpret, Retain))
    assert {role: item.descriptor.kind for role, item in roles.items()} == {
        OperationRole.RENDER_ARTIFACTS: "demo.render_role_artifacts",
        OperationRole.VERIFY_REVISION: "demo.verify_parent_revision",
        OperationRole.INTERPRET_EVIDENCE: "demo.interpret_evidence",
        OperationRole.RETAIN_REVISION: "demo.retain_verified_revision",
    }
