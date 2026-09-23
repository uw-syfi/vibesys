"""Read-model compatibility for version 4 run manifests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api import OrchestrationRegistry
from vibesys.api.contracts import LoopKind, RunStatus
from vibesys.api.store import open_run_store, portable_history_snapshots
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

    assert direct.loop == LoopKind.PLAIN
    assert type(direct.loop) is str
    assert direct.status is RunStatus.UNKNOWN
    assert direct.run_id == manifest.run_id
    assert direct.projection is None
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

    assert direct.loop == LoopKind.EVOLVE
    assert direct.status is RunStatus.UNKNOWN
    assert direct.run_id == manifest.run_id
    assert direct.projection is None
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
    assert direct.projection is None
    assert store.list_runs() == [direct]
    project.state.portable_namespace(manifest.run_id, "evidence").write_bytes(
        "notes.txt", b"candidate review"
    )
    assert portable_history_snapshots(project, manifest.run_id) == ()

    class EvidencePolicy:
        def execute(self, request: object, runtime: object) -> bool:
            del request, runtime
            return True

        def history_namespaces(self) -> tuple[str, ...]:
            return ("evidence",)

    registry = OrchestrationRegistry()
    registry.register("team-search", EvidencePolicy())
    snapshots = portable_history_snapshots(project, manifest.run_id, registry=registry)
    assert [file.relative_path.name for file in snapshots[0].files] == ["notes.txt"]


def test_history_snapshots_follow_the_policy_namespace(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    project.state.create_project("profile project")
    manifest = project.state.new_orchestration_run_manifest(
        "profile run",
        run_id="profile-run",
        branch="vibesys-runs/profile-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        orchestration=OrchestrationDescriptor(id="profile-guided", config_version=1, options={}),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    project.state.portable_namespace(manifest.run_id, "agent").write_bytes("agent.json", b"{}")
    project.state.portable_namespace(manifest.run_id, "plain").write_bytes("plain.json", b"{}")

    snapshots = portable_history_snapshots(project, manifest.run_id)

    assert len(snapshots) == 1
    assert [file.relative_path.name for file in snapshots[0].files] == ["agent.json"]
