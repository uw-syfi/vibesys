"""Every ValidationPasses implementation passes the shared pass-store contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record
from vs_runtime.api.infrastructure import StateValidationPasses
from vs_runtime.api.testing import MemoryValidationPasses
from vs_runtime.api.validation_passes_contracts import check_validation_passes

if TYPE_CHECKING:
    from pathlib import Path


def _project_run(tmp_path: Path) -> tuple[Project, str]:
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    project.state.create_project("Passes", now=now)
    manifest = project.state.new_run_manifest(
        "Passes",
        branch="vibesys/passes",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="team-search", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=now,
        unique=UUID(int=1),
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def test_memory_passes_meet_the_contract() -> None:
    store = MemoryValidationPasses()
    check_validation_passes(lambda: store, durable=False)


def test_state_passes_meet_the_contract(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path)
    check_validation_passes(
        lambda: StateValidationPasses(project.state.local_namespace(run_id, "validation")),
        durable=True,
    )


def test_state_passes_write_nothing_in_the_project_tree(tmp_path: Path) -> None:
    project, run_id = _project_run(tmp_path)
    before = sorted(project.root.rglob("*"))
    store = StateValidationPasses(project.state.local_namespace(run_id, "validation"))
    check_validation_passes(lambda: store, durable=False)
    assert sorted(project.root.rglob("*")) == before
