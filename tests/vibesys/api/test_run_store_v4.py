"""Read-model compatibility for version 4 run manifests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api.contracts import LoopKind, RunStatus
from vibesys.api.store import open_run_store
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path


def test_plain_v4_run_remains_visible_in_run_store(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Implement the service.\n")
    project = Project.open(tmp_path)
    project.state.create_project("plain project")
    manifest = project.state.new_orchestration_run_manifest(
        "plain run",
        run_id="plain-run",
        branch="vibesys-runs/plain-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        orchestration=OrchestrationDescriptor(
            id="plain", config_version=1, options={"max_rounds": 3}
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)

    store = open_run_store(project)
    direct = store.get_run(manifest.run_id)
    listed = store.list_runs()

    assert direct.loop is LoopKind.PLAIN
    assert direct.status is RunStatus.UNKNOWN
    assert direct.run_id == manifest.run_id
    assert direct.rounds == []
    assert len(listed) == 1
    assert listed[0] == direct


def test_evolve_v4_run_remains_visible_in_run_store(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Optimize the service.\n")
    project = Project.open(tmp_path)
    project.state.create_project("evolve project")
    manifest = project.state.new_orchestration_run_manifest(
        "evolve run",
        run_id="evolve-run",
        branch="vibesys-runs/evolve-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        orchestration=OrchestrationDescriptor(
            id="evolve", config_version=1, options={"max_generations": 3}
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)

    store = open_run_store(project)
    direct = store.get_run(manifest.run_id)

    assert direct.loop is LoopKind.EVOLVE
    assert direct.status is RunStatus.UNKNOWN
    assert direct.run_id == manifest.run_id
    assert direct.rounds == []
    assert store.list_runs() == [direct]


def test_unknown_v4_run_has_generic_history_view(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Explore candidates.\n")
    project = Project.open(tmp_path)
    project.state.create_project("custom project")
    manifest = project.state.new_orchestration_run_manifest(
        "custom run",
        run_id="team-run",
        branch="vibesys-runs/team-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        orchestration=OrchestrationDescriptor(
            id="team-search", config_version=2, options={"workers": 3}
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)

    store = open_run_store(project)
    direct = store.get_run(manifest.run_id)

    assert direct.loop == "team-search"
    assert direct.status is RunStatus.UNKNOWN
    assert direct.rounds == []
    assert store.list_runs() == [direct]
