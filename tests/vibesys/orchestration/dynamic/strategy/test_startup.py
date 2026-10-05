"""Production startup accepts the strategy's own declaration, not a mirror of it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.strategy._run import config

from vibesys.orchestration.dynamic.strategy.api import (
    DynamicStrategy,
    dynamic_operation_registrations,
)
from vs_core.api import Capabilities, validate_startup

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
    raises=ImportError,
    reason="needs the operation owner wiring and require_owned, #1312",
)
def test_production_owners_cover_every_operation_the_strategy_requires(tmp_path: Path) -> None:
    from vs_project.api import Project  # noqa: PLC0415
    from vs_prompts.api import TemplateRenderer  # noqa: PLC0415
    from vs_runtime.api import ArtifactStore  # noqa: PLC0415
    from vs_runtime.api.core import (  # noqa: PLC0415
        OperationPorts,
        OperationRole,
        bind_operations,
        build_operation_catalog,
        production_owners,
    )
    from vs_runtime.api.testing import (  # noqa: PLC0415
        FakeEvidenceLedger,
        FakeWorkspace,
        FakeWorkspaces,
    )

    (tmp_path / "project").mkdir()
    namespace = Project.open(tmp_path / "project").state.state_store_namespace("run")
    registrations = {
        OperationRole[_ROLES[item.descriptor.kind]]: item
        for item in dynamic_operation_registrations()
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
