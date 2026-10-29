"""Read-model behavior for current version 5 run manifests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.core_plugins import capability_plugin
from tests.support.real_git import REAL_GIT, real_git_tracker

from launch import open_run_store
from vibesys.api import OrchestrationRegistry
from vibesys.api.contracts import RunStatus
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
)
from vs_project.api.testing import run_execution_record
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import RunStore


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


def _store(project: Project, *, registry: OrchestrationRegistry | None = None) -> RunStore:
    """The product run store over ``project``, reading its history with real Git."""
    return open_run_store(project, registry=registry, git=REAL_GIT, threads=SimThreads())


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4])
def test_public_run_store_rejects_pre_v5_manifests_explicitly(
    tmp_path: Path,
    schema_version: int,
) -> None:
    project = Project.open(tmp_path)
    project.state.create_project("legacy run project", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "legacy run",
        run_id=f"v{schema_version}-run",
        branch=f"vibesys-runs/v{schema_version}-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="plain", config_version=1, options={}),
        trusted_input_baseline="0" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    manifest_path = tmp_path / ".vibesys" / "state" / "runs" / manifest.run_id / "run.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    store = _store(project)
    with pytest.raises(
        ProjectStateError,
        match=rf"unsupported run schema version {schema_version}.*requires version 5",
    ):
        store.get_run(manifest.run_id)


def test_plain_v5_run_is_visible_in_run_store(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Implement the service.\n")
    project = Project.open(tmp_path)
    project.state.create_project("plain project", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "plain run",
        run_id="plain-run",
        branch="vibesys-runs/plain-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
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
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    # A plain run is a core run: its state is the runtime record, and a run that committed none
    # is listed with its identity and no projection.
    store = _store(project)
    direct = store.get_run(manifest.run_id)
    listed = store.list_runs()

    assert direct.loop == "plain"
    assert type(direct.loop) is str
    assert direct.status is RunStatus.UNKNOWN
    assert direct.run_id == manifest.run_id
    assert not direct.projection
    assert len(listed) == 1
    assert listed[0] == direct
    assert store.get_record(manifest.run_id).facts().model_dump() == {
        "trusted_input_baseline": "0" * 40,
        "effective_objective": None,
    }


def test_unknown_v5_run_has_generic_history_view(tmp_path: Path) -> None:
    (tmp_path / "OBJECTIVE.md").write_text("Explore candidates.\n")
    project = Project.open(tmp_path)
    project.state.create_project("custom project", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "custom run",
        run_id="team-run",
        branch="vibesys-runs/team-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="team-search", config_version=2, options={"workers": 3}
        ),
        trusted_input_baseline="0" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)

    store = _store(project)
    direct = store.get_run(manifest.run_id)

    assert direct.loop == "team-search"
    assert direct.status is RunStatus.UNKNOWN
    assert direct.projection is None
    assert store.list_runs() == [direct]
    project.state.portable_namespace(manifest.run_id, "team-search").write_bytes(
        "notes.txt", b"candidate review"
    )
    assert _store(project).get_record(manifest.run_id).history_documents() == ()


def test_workspace_changes_hide_the_registered_plugins_memory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "service.py").write_text("VALUE = 1\n")
    _git(root, "add", "service.py")
    _git(root, "commit", "-q", "-m", "baseline")
    baseline = _git(root, "rev-parse", "HEAD")

    project = Project.open(root)
    project.state.create_project("custom memory project", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "custom memory run",
        run_id="custom-memory-run",
        branch="vibesys-runs/custom-memory-run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="memory-policy", config_version=1, options={}),
        trusted_input_baseline=baseline,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)

    (root / "private-memory").mkdir()
    (root / "private-memory" / "notes.md").write_text("framework notes\n")
    (root / "service.py").write_text("VALUE = 2\n")
    _git(root, "add", "private-memory/notes.md", "service.py")
    _git(root, "commit", "-q", "-m", "candidate")
    head = _git(root, "rev-parse", "HEAD")

    registry = OrchestrationRegistry()
    registry.register_plugin(
        capability_plugin("memory-policy", artifact_directories=("private-memory",))
    )

    changes = (
        _store(project, registry=registry)
        .get_record(manifest.run_id)
        .workspace_changes(baseline, head)
    )

    assert [change.path for change in changes] == ["service.py"]


def _run(root: Path, run_id: str, minute: int, objective: str) -> None:
    """A run started and committed as the product does, its objective recorded."""
    tracker = real_git_tracker(root, run_id=run_id, events=NullGitTrackerEvents())
    tracker.init(existing=False)
    project = Project.open(root)
    now = datetime(2026, 1, 1, 0, minute, tzinfo=UTC)
    project.state.create_project("two runs", now=now)
    project.state.create_run(
        project.state.new_run_manifest(
            f"run {run_id}",
            run_id=run_id,
            branch=tracker.project_branch,
            vibesys_version="test",
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="plain", config_version=1, options={}),
            trusted_input_baseline="0" * 40,
            now=now,
        )
    )
    tracker.snapshot_with_framework_metadata(
        f"vibesys: initialize run {run_id}", project.state.initialization_snapshot(run_id)
    )
    runtime = project.state.portable_namespace(run_id, "runtime")
    runtime.write_bytes("effective-objective.md", objective.encode())
    tracker.snapshot_framework_state("vibesys: record effective objective", runtime.snapshot())


@pytest.mark.parametrize("checked_out", ["vibesys-runs/second", "main"])
def test_every_run_lists_and_opens_whichever_branch_is_checked_out(
    tmp_path: Path, checked_out: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "service.py").write_text("VALUE = 1\n")
    _git(root, "add", "service.py")
    _git(root, "commit", "-q", "-m", "baseline")
    _run(root, "first", 0, "Make it fast.\n")
    _run(root, "second", 1, "Make it faster.\n")
    assert _git(root, "branch", "--show-current") == "vibesys-runs/second"
    # The second run started from the user's branch, not from the first run's.
    assert _git(root, "merge-base", "vibesys-runs/first", "vibesys-runs/second") == _git(
        root, "rev-parse", "main"
    )
    _git(root, "switch", "-q", checked_out)

    store = _store(Project.open(root))

    assert [view.run_id for view in store.list_runs()] == ["second", "first"]
    for run_id, objective in (("first", "Make it fast.\n"), ("second", "Make it faster.\n")):
        record = store.get_record(run_id)
        assert record.facts().effective_objective == objective
        paths = {document.relative_path.as_posix() for document in record.portable_documents()}
        assert {"run.json", "runtime/effective-objective.md"} <= paths
