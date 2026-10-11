"""A strategy's four operations register under its namespace and map onto the runtime roles."""

import re
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel

from vs_core.api import (
    EvidenceReadingsBase,
    InterpretEvidenceBase,
    LifecycleClass,
    OperationKindError,
    OperationRegistry,
    RenderRoleArtifactsBase,
    RetainVerifiedRevisionBase,
    RevisionAuthority,
    StrategyOperations,
    Value,
    VerifyParentRevisionBase,
)


class Context(Value):
    text: str = ""


class Reading(Value):
    number: float = 0.0


class Readings(EvidenceReadingsBase[Reading]):
    pass


class DemoRender(RenderRoleArtifactsBase[Context]):
    kind: Literal["demo.render_role_artifacts"] = "demo.render_role_artifacts"


class DemoVerify(VerifyParentRevisionBase):
    kind: Literal["demo.verify_parent_revision"] = "demo.verify_parent_revision"


class DemoInterpret(InterpretEvidenceBase):
    kind: Literal["demo.interpret_evidence"] = "demo.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = Readings


class DemoRetain(RetainVerifiedRevisionBase):
    kind: Literal["demo.retain_verified_revision"] = "demo.retain_verified_revision"


class OtherRender(RenderRoleArtifactsBase[Context]):
    kind: Literal["other.render_role_artifacts"] = "other.render_role_artifacts"


class OtherVerify(VerifyParentRevisionBase):
    kind: Literal["other.verify_parent_revision"] = "other.verify_parent_revision"


class OtherInterpret(InterpretEvidenceBase):
    kind: Literal["other.interpret_evidence"] = "other.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = Readings


class OtherRetain(RetainVerifiedRevisionBase):
    kind: Literal["other.retain_verified_revision"] = "other.retain_verified_revision"


OPERATIONS = {
    "demo": StrategyOperations("demo", DemoRender, DemoVerify, DemoInterpret, DemoRetain),
    "other": StrategyOperations("other", OtherRender, OtherVerify, OtherInterpret, OtherRetain),
}


@pytest.mark.parametrize("namespace", ["demo", "other"])
def test_the_kinds_schemas_and_roles_follow_the_namespace(namespace: str) -> None:
    operations = OPERATIONS[namespace]
    kinds = [registration.descriptor.kind for registration in operations.registrations()]
    assert kinds == [
        f"{namespace}.render_role_artifacts",
        f"{namespace}.verify_parent_revision",
        f"{namespace}.interpret_evidence",
        f"{namespace}.retain_verified_revision",
    ]
    render, verify, _, retain = (item.descriptor for item in operations.registrations())
    assert retain.revision_authority is RevisionAuthority.RETAIN
    assert verify.lifecycle is LifecycleClass.QUERY
    assert not verify.inspect
    assert render.inspect
    assert render.request_schema.name == f"{namespace}.render-role-artifacts"
    assert isinstance(operations.registry(), OperationRegistry)
    assert operations.schema_ref(kinds[0]).request_schema.name.startswith(namespace)


def test_two_namespaces_never_share_a_kind() -> None:
    first = {r.descriptor.kind for r in OPERATIONS["demo"].registrations()}
    second = {r.descriptor.kind for r in OPERATIONS["other"].registrations()}
    assert not first & second


def test_a_kind_outside_the_namespace_is_refused() -> None:
    with pytest.raises(OperationKindError, match=re.escape("demo.render_role_artifacts")):
        StrategyOperations("demo", OtherRender, DemoVerify, DemoInterpret, DemoRetain)
