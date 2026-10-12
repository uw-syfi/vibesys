"""The runtime performs a strategy's operations by role, whatever the strategy names them."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from vs_core.api import (
    CaptureCommandBase,
    EvidenceReadingsBase,
    ExportPatchBase,
    InterpretEvidenceBase,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    StrategyOperations,
    ValidateLocalBase,
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


class Validate(ValidateLocalBase):
    kind: Literal["demo.validate_local"] = "demo.validate_local"


class Export(ExportPatchBase):
    kind: Literal["demo.export_patch"] = "demo.export_patch"


def test_an_optional_capture_maps_to_its_own_role_and_is_refused_without_a_runner() -> None:
    operations = StrategyOperations(
        "demo",
        Render,
        Verify,
        Interpret,
        Retain,
        capture=Capture,
        validate_local=Validate,
        export_patch=Export,
    )
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


def test_each_optional_operation_maps_to_its_own_role_whichever_others_are_declared() -> None:
    only_validate = operation_roles(
        StrategyOperations("demo", Render, Verify, Interpret, Retain, validate_local=Validate)
    )
    assert only_validate[OperationRole.VALIDATE_LOCAL].descriptor.kind == "demo.validate_local"
    assert OperationRole.CAPTURE_COMMAND not in only_validate
    only_capture = operation_roles(
        StrategyOperations("demo", Render, Verify, Interpret, Retain, capture=Capture)
    )
    assert only_capture[OperationRole.CAPTURE_COMMAND].descriptor.kind == "demo.capture_command"
    assert OperationRole.VALIDATE_LOCAL not in only_capture


def test_validation_is_refused_without_a_runner_and_isolated_candidates() -> None:
    operations = StrategyOperations(
        "demo", Render, Verify, Interpret, Retain, validate_local=Validate
    )
    roles = operation_roles(operations)
    catalog = build_operation_catalog(
        {OperationRole.VALIDATE_LOCAL: roles[OperationRole.VALIDATE_LOCAL]}, {}
    )
    (entry,) = catalog.entries
    assert entry.owner is None
    assert entry.refusal is RefusalReason.NO_COMMAND_RUNNER
