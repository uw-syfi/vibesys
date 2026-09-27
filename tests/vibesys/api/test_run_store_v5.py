"""Read-model behavior for current version 5 run manifests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from tests.support.run_execution import run_execution_record

from vibesys.api import OrchestrationRegistry
from vibesys.api.contracts import RunStatus
from vibesys.api.store import open_run_store, portable_history_snapshots
from vibesys.orchestration.evolve.models import EvolveState
from vibesys.orchestration.issue_queue import IssueQueueState
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_runtime.api import OrchestrationPlugin, RunHost
from vs_runtime.api import RunStatus as PluginRunStatus

if TYPE_CHECKING:
    from pathlib import Path


def test_plain_v5_run_is_visible_in_run_store(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Implement the service.\n")
    project = Project.open(tmp_path)
    project.state.create_project("plain project")
    manifest = project.state.new_run_manifest(
        "plain run",
        run_id="plain-run",
        branch="vibesys-runs/plain-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="plain",
            config_version=1,
            options={
                "max_rounds": 3,
                "max_attempts_per_issue": 2,
                "max_issues_per_perf_eval": 2,
            },
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    expected = IssueQueueState(round_idx=1, bootstrap_done=True)
    project.state.portable_namespace(manifest.run_id, "plain").slot(
        "state.json", IssueQueueState
    ).save(expected)

    store = open_run_store(project)
    direct = store.get_run(manifest.run_id)
    listed = store.list_runs()

    assert direct.loop == "plain"
    assert type(direct.loop) is str
    assert direct.status is RunStatus.UNKNOWN
    assert direct.run_id == manifest.run_id
    assert direct.projection == expected.model_dump(mode="json")
    assert len(listed) == 1
    assert listed[0] == direct


def test_evolve_v5_run_is_visible_in_run_store(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Optimize the service.\n")
    project = Project.open(tmp_path)
    project.state.create_project("evolve project")
    manifest = project.state.new_run_manifest(
        "evolve run",
        run_id="evolve-run",
        branch="vibesys-runs/evolve-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="evolve", config_version=1, options={"max_generations": 3}
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    state = EvolveState.model_validate({"population": {"rng_state": (3, (), None)}})
    project.state.portable_namespace(manifest.run_id, "evolve").slot(
        "state.json", EvolveState
    ).save(state)

    store = open_run_store(project)
    direct = store.get_run(manifest.run_id)

    assert direct.loop == "evolve"
    assert direct.status is RunStatus.UNKNOWN
    assert direct.run_id == manifest.run_id
    assert direct.projection == {
        "population": state.population.model_dump(mode="json"),
        "metric_space": state.metric_space.model_dump(mode="json"),
        "generation": 0,
    }
    assert store.list_runs() == [direct]


def test_unknown_v5_run_has_generic_history_view(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Explore candidates.\n")
    project = Project.open(tmp_path)
    project.state.create_project("custom project")
    manifest = project.state.new_run_manifest(
        "custom run",
        run_id="team-run",
        branch="vibesys-runs/team-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
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
    project.state.portable_namespace(manifest.run_id, "team-search").write_bytes(
        "notes.txt", b"candidate review"
    )
    assert portable_history_snapshots(project, manifest.run_id) == ()

    class EvidenceOptions(BaseModel):
        model_config = ConfigDict(extra="forbid")

        workers: int

    class EvidenceState(BaseModel):
        revision: int = 0

    async def run_evidence(host: RunHost, options: BaseModel) -> PluginRunStatus:
        del host, options
        return PluginRunStatus.SUCCEEDED

    registry = OrchestrationRegistry()
    registry.register_plugin(
        OrchestrationPlugin(
            id="team-search",
            agents=(),
            options=EvidenceOptions,
            orchestrate=run_evidence,
            state=EvidenceState,
            config_version=2,
        )
    )
    snapshots = portable_history_snapshots(project, manifest.run_id, registry=registry)
    assert [file.relative_path.name for file in snapshots[0].files] == ["notes.txt"]


def test_history_snapshots_follow_the_policy_namespace(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    project.state.create_project("profile project")
    manifest = project.state.new_run_manifest(
        "profile run",
        run_id="profile-run",
        branch="vibesys-runs/profile-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="profile-guided-multi-agent", config_version=1, options={}
        ),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    project.state.portable_namespace(manifest.run_id, "profile-guided-multi-agent").write_bytes(
        "agent.json", b"{}"
    )
    project.state.portable_namespace(manifest.run_id, "plain").write_bytes("plain.json", b"{}")

    snapshots = portable_history_snapshots(project, manifest.run_id)

    assert len(snapshots) == 1
    assert [file.relative_path.name for file in snapshots[0].files] == ["agent.json"]
