"""Production startup accepts the strategy's own declaration, not a mirror of it."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pytest
from tests.vibesys.orchestration.dynamic.strategy._run import config

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    dynamic_operation_registrations,
)
from vs_core.api import Capabilities, validate_startup
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore

if TYPE_CHECKING:
    from pathlib import Path

_ROLES = {
    "dynamic.render_role_artifacts": "RENDER_ARTIFACTS",
    "dynamic.verify_parent_revision": "VERIFY_REVISION",
    "dynamic.interpret_evidence": "INTERPRET_EVIDENCE",
    "dynamic.retain_verified_revision": "RETAIN_REVISION",
}


@pytest.mark.xfail(
    strict=True,
    raises=(ImportError, AttributeError),
    reason="needs the operation owner wiring and require_owned, #1312",
)
def test_production_owners_cover_every_operation_the_strategy_requires(tmp_path: Path) -> None:
    # Imported by name: on main `vs_runtime.api.core` lacks the wiring this test needs.
    core: Any = importlib.import_module("vs_runtime.api.core")
    testing: Any = importlib.import_module("vs_runtime.api.testing")
    role = core.OperationRole
    (tmp_path / "project").mkdir()
    namespace = Project.open(tmp_path / "project").state.state_store_namespace("run")
    registrations = {
        role[_ROLES[item.descriptor.kind]]: item for item in dynamic_operation_registrations()
    }
    workspaces = testing.FakeWorkspaces(testing.FakeWorkspace())
    ports = core.OperationPorts(
        renderer=TemplateRenderer(tmp_path),
        artifacts=ArtifactStore(namespace),
        workspaces=workspaces,
        ledger=workspaces,
        evidence=testing.FakeEvidenceLedger(),
        commit_of=lambda revision: revision.git_commit,
        retention_label="verified",
    )
    catalog = core.build_operation_catalog(
        registrations, core.production_owners(registrations, ports)
    )
    declaration = DynamicStrategy(config=config()).declaration
    catalog.require_owned(declaration)
    validate_startup(declaration, Capabilities(operations=catalog.offered_operations))
    core.bind_operations(declaration, catalog, namespace)
