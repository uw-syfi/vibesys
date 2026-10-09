"""Startup gate: a strategy's required operations need real owners, never refusals."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.runtime_operations import SCENARIO_NAMES, OperationScenario, scenarios

from vs_core.api import (
    Capabilities,
    ContractError,
    OperationRegistry,
    SchemaRef,
    StrategyDeclaration,
    StrategyId,
    validate_startup,
)
from vs_project.api import Project
from vs_runtime.api.core import (
    OperationCatalog,
    OperationRole,
    RegisteredOperationRequests,
    bind_operations,
    build_operation_catalog,
)

_STRATEGY_NAMES = tuple(name for name in SCENARIO_NAMES if name != "echo")


def _catalog_and_namespace(
    root: Path,
) -> tuple[tuple[OperationScenario, ...], OperationCatalog, Project]:
    (root / "project").mkdir()
    project = Project.open(root / "project")
    items = scenarios(root, project.state.state_store_namespace("run"))
    registry = OperationRegistry(tuple(item.entry.registration for item in items))
    return items, OperationCatalog(registry, tuple(item.entry for item in items)), project


def _declaration(
    items: tuple[OperationScenario, ...], required: tuple[str, ...]
) -> StrategyDeclaration:
    by_name = {item.name: item for item in items}
    return StrategyDeclaration(
        strategy_id=StrategyId(root="s"),
        state_schema=SchemaRef(name="state", version=1),
        required_operations=tuple(by_name[name].entry.schema for name in required),
    )


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(required=st.lists(st.sampled_from(_STRATEGY_NAMES), unique=True, max_size=4))
def test_startup_fails_exactly_when_a_required_operation_is_refused(required: list[str]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        items, catalog, project = _catalog_and_namespace(Path(raw))
        declaration = _declaration(items, tuple(required))
        refused = [name for name in required if next(i for i in items if i.name == name).refused]
        namespace = project.state.state_store_namespace("run")
        if not refused:
            bindings = bind_operations(declaration, catalog, namespace)
            assert isinstance(bindings.executors.operations, RegisteredOperationRequests)
            assert bindings.registry is catalog.registry
            return
        kind = next(i for i in items if i.name == refused[0]).entry.registration.descriptor.kind
        with pytest.raises(ContractError, match=re.escape(kind)):
            bind_operations(declaration, catalog, namespace)


def test_a_refused_operation_is_registered_but_never_offered() -> None:
    with tempfile.TemporaryDirectory() as raw:
        items, catalog, _ = _catalog_and_namespace(Path(raw))
        offered = {descriptor.kind for descriptor in catalog.offered_operations}
        for item in items:
            kind = item.entry.registration.descriptor.kind
            assert catalog.find(item.entry.schema) is item.entry
            assert (kind in offered) is (not item.refused)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(required=st.lists(st.sampled_from(_STRATEGY_NAMES), unique=True, max_size=4))
def test_core_startup_agrees_with_the_catalog_on_what_may_be_required(required: list[str]) -> None:
    with tempfile.TemporaryDirectory() as raw:
        items, catalog, _ = _catalog_and_namespace(Path(raw))
        declaration = _declaration(items, tuple(required))
        offered = Capabilities(operations=catalog.offered_operations)
        refused = any(next(i for i in items if i.name == name).refused for name in required)
        if refused:
            with pytest.raises(ContractError):
                validate_startup(declaration, offered)
            with pytest.raises(ContractError):
                catalog.require_owned(declaration)
        else:
            catalog.require_owned(declaration)
            selected = validate_startup(declaration, offered).operations
            assert len(selected) == len(required)


def test_an_unregistered_required_operation_is_named() -> None:
    with tempfile.TemporaryDirectory() as raw:
        items, _, _ = _catalog_and_namespace(Path(raw))
        other = next(i for i in items if i.name == "echo")
        smaller = OperationCatalog(OperationRegistry((other.entry.registration,)), (other.entry,))
        declaration = _declaration(items, ("render",))
        with pytest.raises(ContractError, match=r"test\.render"):
            smaller.require_owned(declaration)


def test_builder_rejects_an_owner_without_a_registration_and_an_ownerless_unrefusable_role() -> (
    None
):
    with tempfile.TemporaryDirectory() as raw:
        items, _, _ = _catalog_and_namespace(Path(raw))
        verify = next(i for i in items if i.name == "verify").entry
        owner = verify.owner
        assert owner is not None
        with pytest.raises(ContractError, match="no registration"):
            build_operation_catalog({}, {OperationRole.VERIFY_REVISION: owner})
        with pytest.raises(ContractError, match="cannot be refused"):
            build_operation_catalog({OperationRole.VERIFY_REVISION: verify.registration}, {})
