"""Read-model behavior for current version 5 run manifests."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict
from tests.support import run_test_command
from tests.support.run_execution import run_execution_record

from vibesys.api import OrchestrationRegistry, open_run_store
from vibesys.api.contracts import RunStatus
from vibesys.orchestration.evolve.models import EvolveState
from vibesys.orchestration.issue_queue import IssueQueueState
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
)
from vs_runtime.api import OrchestrationPlugin, RunHost
from vs_runtime.api import RunStatus as PluginRunStatus

if TYPE_CHECKING:
    from pathlib import Path


_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str) -> str:
    return run_test_command(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY},
    ).stdout.strip()


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4])
def test_public_run_store_rejects_pre_v5_manifests_explicitly(
    tmp_path: Path,
    schema_version: int,
) -> None:
    project = Project.open(tmp_path)
    project.state.create_project("legacy run project")
    manifest = project.state.new_run_manifest(
        "legacy run",
        run_id=f"v{schema_version}-run",
        branch=f"vibesys-runs/v{schema_version}-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="plain", config_version=1, options={}),
        trusted_input_baseline="0" * 40,
    )
    project.state.create_run(manifest)
    manifest_path = tmp_path / ".vibesys" / "state" / "runs" / manifest.run_id / "run.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    store = open_run_store(project)
    with pytest.raises(
        ProjectStateError,
        match=rf"unsupported run schema version {schema_version}.*requires version 5",
    ):
        store.get_run(manifest.run_id)


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
    assert store.get_record(manifest.run_id).facts().model_dump() == {
        "trusted_input_baseline": "0" * 40,
        "effective_objective": None,
    }


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
    assert open_run_store(project).get_record(manifest.run_id).history_documents() == ()

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
    documents = (
        open_run_store(project, registry=registry).get_record(manifest.run_id).history_documents()
    )
    assert [document.relative_path.name for document in documents] == ["notes.txt"]


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

    documents = open_run_store(project).get_record(manifest.run_id).history_documents()

    assert [document.relative_path.name for document in documents] == ["agent.json"]


def test_workspace_changes_hide_the_registered_plugins_memory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "service.py").write_text("VALUE = 1\n")
    _git(root, "add", "service.py")
    _git(root, "commit", "-q", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")

    project = Project.open(root)
    project.state.create_project("custom memory project")
    manifest = project.state.new_run_manifest(
        "custom memory run",
        run_id="custom-memory-run",
        branch="vibesys-runs/custom-memory-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="memory-policy", config_version=1, options={}),
        trusted_input_baseline=baseline,
    )
    project.state.create_run(manifest)

    (root / "private-memory").mkdir()
    (root / "private-memory" / "notes.md").write_text("framework notes\n")
    (root / "service.py").write_text("VALUE = 2\n")
    _git(root, "add", "private-memory/notes.md", "service.py")
    _git(root, "commit", "-q", "-m", "candidate")
    head = _git(root, "rev-parse", "HEAD")

    class MemoryOptions(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def run_memory(host: RunHost, options: BaseModel) -> PluginRunStatus:
        del host, options
        return PluginRunStatus.SUCCEEDED

    registry = OrchestrationRegistry()
    registry.register_plugin(
        OrchestrationPlugin(
            id="memory-policy",
            agents=(),
            options=MemoryOptions,
            orchestrate=run_memory,
            memory_paths=("private-memory",),
        )
    )

    changes = (
        open_run_store(project, registry=registry)
        .get_record(manifest.run_id)
        .workspace_changes(baseline, head)
    )

    assert [change.path for change in changes] == ["service.py"]
