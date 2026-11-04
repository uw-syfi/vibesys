"""The run stores the startup selection, which is not the codec's registration order."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

import pytest

import vs_core.api as core
from vs_core.api.search import search
from vs_core.api.testing import Harness, new_run

from .flow_support import FACTS, Config
from .test_flows_owners import NOTES

if TYPE_CHECKING:
    from pydantic import BaseModel


class _Context(core.Value):
    text: str = ""


class _Reading(core.Value):
    number: float = 0.0


class _Readings(core.EvidenceReadingsBase[_Reading]):
    pass


class _Render(core.RenderRoleArtifactsBase[_Context]):
    kind: Literal["order.render_role_artifacts"] = "order.render_role_artifacts"


class _Verify(core.VerifyParentRevisionBase):
    kind: Literal["order.verify_parent_revision"] = "order.verify_parent_revision"


class _Interpret(core.InterpretEvidenceBase):
    kind: Literal["order.interpret_evidence"] = "order.interpret_evidence"
    outcome_model: ClassVar[type[BaseModel]] = _Readings


class _Capture(core.CaptureCommandBase):
    kind: Literal["order.capture_command"] = "order.capture_command"


class _Validate(core.ValidateLocalBase):
    kind: Literal["order.validate_local"] = "order.validate_local"


def _kinds(operations: tuple[core.OperationDescriptor, ...]) -> list[str]:
    return [item.kind for item in operations]


def _started(
    world: Literal["capture-and-owned", "capture-validate-owned"],
) -> tuple[core.CoreState, tuple[core.OperationDescriptor, ...]]:
    operations = core.StrategyOperations(
        "order",
        _Render,
        _Verify,
        _Interpret,
        capture=_Capture,
        validate_local=_Validate if world == "capture-validate-owned" else None,
    )
    name = "order-both" if world == "capture-validate-owned" else "order-capture"

    @search(name, config=Config, operations=operations, owned=(NOTES,))
    async def body(_ctx: core.Flow, _config: Config) -> None:
        return None

    codec = body.registry()
    assert codec is not None
    state = new_run(body.driver(Config(), codec), Harness(registry=codec, facts=FACTS))
    return state, codec.descriptors


@pytest.mark.regression
@pytest.mark.parametrize("world", ["capture-and-owned", "capture-validate-owned"])
def test_the_durable_registry_is_the_selected_operations(
    world: Literal["capture-and-owned", "capture-validate-owned"],
) -> None:
    """Single keeps an optional capture and an owned operation; multi keeps capture,
    local validation and an owned operation. Startup selects the owned operation
    before the optional ones, and the codec lists the optional ones first.
    """
    state, codec = _started(world)
    assert state.registry == state.run.capabilities.operations
    assert _kinds(state.registry) != _kinds(codec)
    owned = _kinds(state.registry).index(NOTES.descriptor.kind)
    optional = _kinds(state.registry).index("order.capture_command")
    assert owned < optional
