"""Production startup accepts the strategy's own declaration, not a mirror of it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.orchestration.dynamic.strategy._run import config

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    dynamic_operation_registrations,
)
from vs_core.api import Capabilities, validate_startup
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import (
    OperationPorts,
    OperationRole,
    bind_operations,
    build_operation_catalog,
    production_owners,
)
from vs_runtime.api.testing import FakeEvidenceLedger, FakeWorkspace, FakeWorkspaces

if TYPE_CHECKING:
    from pathlib import Path

_ROLES = {
    "dynamic.render_role_artifacts": OperationRole.RENDER_ARTIFACTS,
    "dynamic.verify_parent_revision": OperationRole.VERIFY_REVISION,
    "dynamic.interpret_evidence": OperationRole.INTERPRET_EVIDENCE,
    "dynamic.retain_verified_revision": OperationRole.RETAIN_REVISION,
}


def test_production_owners_cover_every_operation_the_strategy_requires(tmp_path: Path) -> None:
    (tmp_path / "project").mkdir()
    namespace = Project.open(tmp_path / "project").state.state_store_namespace("run")
    registrations = {
        _ROLES[item.descriptor.kind]: item for item in dynamic_operation_registrations()
    }
    workspaces = FakeWorkspaces(FakeWorkspace())
    ports = OperationPorts(
        renderer=TemplateRenderer(tmp_path),
        artifacts=ArtifactStore(namespace),
        workspaces=workspaces,
        ledger=workspaces,
        evidence=FakeEvidenceLedger(),
        commit_of=lambda revision: revision.git_commit,
        retention_label="verified",
    )
    catalog = build_operation_catalog(registrations, production_owners(registrations, ports))
    declaration = DynamicStrategy(config=config()).declaration
    catalog.require_owned(declaration)
    validate_startup(declaration, Capabilities(operations=catalog.offered_operations))
    bind_operations(declaration, catalog, namespace)
