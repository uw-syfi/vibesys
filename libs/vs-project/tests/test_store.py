# Package-boundary tests intentionally inspect private on-disk details.
# ruff: noqa: SLF001

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from tests.support.run_execution import run_execution_record

from vs_project.api import (
    PROJECT_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    ProjectManifest,
    ProjectStateError,
    RunEnvironmentRecord,
    StateFile,
    StateModelNotFoundError,
    StateSnapshot,
    generate_run_id,
    is_project_state_path,
)

NOW = datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC)
UNIQUE = UUID("12345678-1234-5678-1234-567812345678")


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: int
    phase: str


def _descriptor() -> OrchestrationDescriptor:
    return OrchestrationDescriptor(
        id="team-search",
        config_version=1,
        options={"agents": [{"role": "worker", "budget": 4}], "seed": None},
    )


def _store(tmp_path: Path) -> Project:
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    store = Project.open(tmp_path)
    store.state.create_project("Queue SPSC", now=NOW)
    return store


def _run(store: Project, *, minute: int = 0) -> OrchestrationRunManifest:
    created_at = NOW + timedelta(minutes=minute)
    manifest = store.state.new_run_manifest(
        "Queue SPSC",
        branch=f"vibesys/queue-{minute}",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=_descriptor(),
        trusted_input_baseline="a" * 40,
        now=created_at,
        unique=UUID(int=minute + 1),
    )
    store.state.create_run(manifest)
    return manifest


def test_version_4_run_manifest_round_trips_without_loop_configuration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    new_run = _run(store)

    assert store.state.load_run(new_run.run_id) == new_run
    assert {run.schema_version for run in store.state.list_runs()} == {RUN_SCHEMA_VERSION}
    raw = json.loads(store.state._run_manifest_path(new_run.run_id).read_text())
    assert "configuration" not in raw
    assert raw["orchestration"]["id"] == "team-search"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "../unsafe"),
        ("config_version", 0),
        ("options", {"score": float("nan")}),
        ("options", {"bad": object()}),
        ("options", {1: "not a string key"}),
    ],
)
def test_orchestration_descriptor_rejects_invalid_envelope(field: str, value: object) -> None:
    payload = {"id": "team-search", "config_version": 1, "options": {}}
    payload[field] = value

    with pytest.raises(ValidationError):
        OrchestrationDescriptor.model_validate(payload, strict=True)


def test_version_4_run_rejects_unknown_keys_on_load(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    path = store.state._run_manifest_path(run.run_id)
    raw = json.loads(path.read_text())
    raw["orchestration"]["outer_loop"] = "agent"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ProjectStateError, match="outer_loop"):
        store.state.load_run(run.run_id)


@pytest.mark.parametrize("version", [1, 2, 3, 99])
def test_loading_unknown_run_schema_fails_explicitly(tmp_path: Path, version: int) -> None:
    store = _store(tmp_path)
    run = _run(store)
    path = store.state._run_manifest_path(run.run_id)
    raw = json.loads(path.read_text())
    raw["schema_version"] = version
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ProjectStateError, match=f"unsupported run schema version {version}"):
        store.state.load_run(run.run_id)


def test_update_orchestration_preserves_identity_and_rejects_version_change(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    changed = OrchestrationDescriptor(
        id="team-search", config_version=1, options={"agents": [], "seed": 9}
    )

    store.state.update_run_orchestration(run.run_id, changed)

    assert store.state.load_run(run.run_id) == run.model_copy(update={"orchestration": changed})
    with pytest.raises(ProjectStateError, match="cannot change orchestration"):
        store.state.update_run_orchestration(
            run.run_id,
            changed.model_copy(update={"config_version": 2}),
        )


def test_generate_run_id_is_sortable_safe_and_deterministic() -> None:
    run_id = generate_run_id("  Quéúe / SPSC?!  ", now=NOW, unique=UNIQUE)

    assert run_id == "20260811-123456-12345678-queue-spsc"
    assert "/" not in run_id


def test_generate_run_id_rejects_naive_time() -> None:
    naive = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)

    with pytest.raises(ProjectStateError, match="timezone"):
        generate_run_id("queue", now=naive, unique=UNIQUE)


def test_manifests_are_strict_versioned_contracts() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        ProjectManifest.model_validate(
            {
                "schema_version": "1",
                "project_id": "queue-abc",
                "created_at": NOW,
                "initial_input_fingerprint": "a" * 64,
            }
        )
    forbidden_field = {"provider_token": ""}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OrchestrationRunManifest(
            schema_version=RUN_SCHEMA_VERSION,
            run_id="run-1",
            project_id="queue-abc",
            display_name="Queue",
            created_at=NOW,
            input_fingerprint="a" * 64,
            trusted_input_baseline="b" * 40,
            branch="vibesys/run-1",
            vibesys_version="0.2.0",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=run_execution_record(),
            orchestration=_descriptor(),
            **forbidden_field,
        )


def test_create_project_writes_portable_committed_manifest(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "queue.rs").write_text("pub struct Queue;\n", encoding="utf-8")
    store = Project.open(tmp_path)

    manifest = store.state.create_project("Queue SPSC", now=NOW)

    assert store.state.load_project() == manifest
    assert store.state._metadata_gitignore_path.read_text(encoding="utf-8") == "/local/\n"
    raw = json.loads(store.state._project_manifest_path.read_text(encoding="utf-8"))
    assert raw == {
        "created_at": "2026-08-11T12:34:56Z",
        "initial_input_fingerprint": manifest.initial_input_fingerprint,
        "project_id": manifest.project_id,
        "schema_version": PROJECT_SCHEMA_VERSION,
    }
    serialized = store.state._project_manifest_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized
    assert "provider" not in serialized


def test_create_project_is_idempotent_after_source_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.state.load_project()
    (tmp_path / "src.py").write_text("changed = True\n", encoding="utf-8")

    assert (
        store.state.create_project("A different display name", now=NOW + timedelta(days=1))
        == original
    )
    assert store.state._metadata_gitignore_path.read_text(encoding="utf-8") == "/local/\n"


def test_project_discovery_validates_manifests_without_exposing_layout(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    invalid = tmp_path / "invalid"
    for root in (first, second, invalid):
        root.mkdir()
    _store(second)
    _store(first)

    assert Project.is_state_initialized(first)
    assert not Project.is_state_initialized(invalid)
    assert Project.find_state_projects(tmp_path) == (first.resolve(), second.resolve())


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        ("src/queue.py", False),
        (".git/HEAD", False),
        (".vs/project.json", False),
        (".vibesys/tasks/queue/vibesys.input.toml", False),
        ("nested/.vibesys/tasks/queue/OBJECTIVE.md", False),
        (".vibesys/stateful/project.json", False),
        ("nested/.vibesys/state/project.json", True),
        ("agent.toml", False),
        ("nested/.env.local", False),
    ],
)
def test_project_state_path_ownership_is_semantic(
    relative_path: str,
    expected: bool,  # noqa: FBT001
) -> None:
    assert is_project_state_path(relative_path) is expected


def test_semantic_runtime_and_sandbox_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    run_id = "run-1"

    early_log_directory = Project.log_directory_for(project, run_id)
    store = _store(project)
    run = _run(store)

    assert early_log_directory == store.state.log_directory(run_id)
    assert store.state.log_directory(run.run_id).is_dir()
    assert store.state.log_directory(run.run_id).is_relative_to(store.state._local_dir)
    assert store.state.model_cache_directory("huggingface").is_relative_to(store.state._local_dir)
    assert store.state.candidate_worktree_directory(run.run_id, "g1c1").is_relative_to(project)
    assert store.state.sandbox_paths().read_only_path == Path(".vibesys")
    assert store.state.sandbox_paths().hidden_path is None
    git = store.state.git_integration(run.run_id)
    assert git.local_exclude_pattern == "/.vibesys/state/local/"
    assert git.metadata_pathspec == ".vibesys/state"
    assert git.metadata_restore_exclusions == (
        ":(exclude).vibesys",
        ":(exclude).vibesys/**",
    )
    assert git.metadata_clean_exclusion == ".vibesys/"


def test_default_state_home_uses_home_dot_vibesys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIBESYS_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()

    log_directory = Project.log_directory_for(project, "run-1")

    assert log_directory.is_relative_to(tmp_path / "home" / ".vibesys" / "projects")


@pytest.mark.parametrize("configured", ["", "relative/state"])
def test_state_home_rejects_invalid_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", configured)
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ProjectStateError, match="VIBESYS_STATE_HOME"):
        Project.log_directory_for(project, "run-1")


def test_same_named_projects_have_distinct_external_state_directories(tmp_path: Path) -> None:
    first = tmp_path / "first" / "project"
    second = tmp_path / "second" / "project"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    first_log = Project.log_directory_for(first, "run-1")
    second_log = Project.log_directory_for(second, "run-1")

    assert first_log != second_log
    assert first_log.parent.parent.parent.name.startswith("project-")
    assert second_log.parent.parent.parent.name.startswith("project-")


def test_legacy_local_state_moves_without_rewriting_and_leaves_worktrees(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    legacy = project / ".vibesys/state/local"
    logs = legacy / "runs/run-1/logs"
    agent = legacy / "runs/run-1/agent"
    worktree = legacy / "runs/run-1/worktrees/g1c1/workspace"
    logs.mkdir(parents=True)
    agent.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (legacy / "current-run").write_bytes(b"run-1\n")
    (logs / "run-events.jsonl").write_bytes(b'{"type":"server_started"}\n')
    (agent / "active.json").write_bytes(b'{"schema_version":1}\n')
    (worktree / "candidate.py").write_bytes(b"candidate = True\n")

    state = Project.open(project).state

    assert (state._local_dir / "current-run").read_bytes() == b"run-1\n"
    assert (state._local_dir / "runs/run-1/logs/run-events.jsonl").read_bytes() == (
        b'{"type":"server_started"}\n'
    )
    assert (state._local_dir / "runs/run-1/agent/active.json").read_bytes() == (
        b'{"schema_version":1}\n'
    )
    assert not (legacy / "current-run").exists()
    assert not (legacy / "runs/run-1/logs").exists()
    assert not (legacy / "runs/run-1/agent").exists()
    assert (worktree / "candidate.py").read_bytes() == b"candidate = True\n"
    assert state.sandbox_paths().hidden_path == Path(".vibesys/state/local")


def test_log_directory_rejects_symlinked_parent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / ".vibesys/state" / "local").mkdir(parents=True)
    outside.mkdir()
    (project / ".vibesys/state" / "local" / "runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        Project.log_directory_for(project, "run-1")


def test_model_cache_directory_rejects_symlinked_parent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    store.state._local_dir.mkdir(parents=True)
    outside.mkdir()
    (store.state._local_dir / "cache").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        store.state.model_cache_directory("huggingface")


def test_portable_run_export_contains_all_run_documents(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    store.state.portable_namespace(run.run_id, "plain").save(
        "cursor.json",
        _Cursor(round=2, phase="judge"),
    )

    exported = store.state.portable_run_export(run.run_id)

    assert {item.relative_path for item in exported.files} == {
        PurePosixPath("run.json"),
        PurePosixPath("plain/cursor.json"),
    }


def test_portable_run_export_rejects_symlinked_directories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.state._contained_run_dir(run.run_id) / "linked").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ProjectStateError, match="must not contain symlinks"):
        store.state.portable_run_export(run.run_id)


def test_create_project_preserves_existing_metadata_ignore_rules(tmp_path: Path) -> None:
    metadata_dir = tmp_path / ".vibesys/state"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / ".gitignore").write_text("custom.tmp\n", encoding="utf-8")
    store = Project.open(tmp_path)

    store.state.create_project("queue", now=NOW)
    store.state.create_project("queue", now=NOW)

    assert store.state._metadata_gitignore_path.read_text(encoding="utf-8") == (
        "custom.tmp\n/local/\n"
    )


def test_create_project_rejects_symlinked_metadata_root_before_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    store = Project.open(project)
    store.state._config_dir.mkdir()
    store.state._metadata_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ProjectStateError, match=r"metadata root must not be a symlink.*\.vibesys/state"
    ):
        store.state.create_project("Queue SPSC", now=NOW)

    assert list(outside.iterdir()) == []


def test_create_project_rejects_symlinked_configuration_root_before_writing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = Project.open(project)
    store.state._config_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ProjectStateError,
        match=r"configuration root must not be a symlink.*\.vibesys",
    ):
        store.state.create_project("Queue SPSC", now=NOW)

    assert list(outside.iterdir()) == []


def test_create_run_rejects_symlinked_local_root_before_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = _store(project)
    manifest = store.state.new_run_manifest(
        "Queue SPSC",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=_descriptor(),
        trusted_input_baseline="a" * 40,
        now=NOW,
        unique=UNIQUE,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    store.state._local_dir.parent.mkdir(parents=True, exist_ok=True)
    store.state._local_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="local metadata root must not be a symlink"):
        store.state.create_run(manifest)

    assert not (store.state._metadata_dir / "runs").exists()
    assert list(outside.iterdir()) == []


def test_input_fingerprint_tracks_portable_files_only(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "queue.rs"
    source.write_text("one", encoding="utf-8")
    store = Project.open(tmp_path)
    initial = store.state.input_fingerprint()

    excluded_files = [
        tmp_path / ".env",
        tmp_path / ".env.local",
        tmp_path / "agent.toml",
        tmp_path / ".git" / "HEAD",
        tmp_path / ".vibesys/state" / "project.json",
        tmp_path / ".pytest_cache" / "state",
        tmp_path / "__pycache__" / "module.pyc",
    ]
    for path in excluded_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret-or-cache", encoding="utf-8")

    assert store.state.input_fingerprint() == initial
    task = tmp_path / ".vibesys" / "tasks" / "queue" / "OBJECTIVE.md"
    task.parent.mkdir(parents=True)
    task.write_text("Optimize queue throughput.\n", encoding="utf-8")
    with_task = store.state.input_fingerprint()
    assert with_task != initial
    (tmp_path / ".vibesys/state" / "run.json").write_text("generated", encoding="utf-8")
    assert store.state.input_fingerprint() == with_task
    task.write_text("Optimize queue latency.\n", encoding="utf-8")
    assert store.state.input_fingerprint() != with_task
    source.write_text("two", encoding="utf-8")
    assert store.state.input_fingerprint() != initial


def test_run_manifest_and_local_state_use_separate_trees(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)

    assert store.state.load_run(manifest.run_id) == manifest
    assert store.state._run_manifest_path(manifest.run_id) == (
        tmp_path / ".vibesys/state" / "runs" / manifest.run_id / "run.json"
    )
    assert store.state.log_directory(manifest.run_id) == (
        store.state._local_dir / "runs" / manifest.run_id / "logs"
    )
    with pytest.raises(ProjectStateError, match="not agent-visible"):
        store.state.local_namespace(manifest.run_id, "agent").agent_visible_path("active.json")
    assert store.state._round_transaction_path(manifest.run_id) == (
        store.state._local_dir / "runs" / manifest.run_id / "round-transaction.json"
    )
    assert store.state._worktrees_dir(manifest.run_id) == (
        tmp_path / ".vibesys/state" / "local" / "runs" / manifest.run_id / "worktrees"
    )
    assert store.state.log_directory(manifest.run_id).is_dir()
    assert not (tmp_path / ".vibesys/state" / "runs" / manifest.run_id / "agent").exists()
    assert not (store.state._local_dir / "runs" / manifest.run_id / "agent").exists()
    assert not store.state._worktrees_dir(manifest.run_id).exists()
    committed = store.state._run_manifest_path(manifest.run_id).read_text(encoding="utf-8")
    assert str(tmp_path) not in committed
    assert "token" not in committed


def test_run_manifest_round_trips_optional_task_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = store.state.new_run_manifest(
        "queue",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=_descriptor(),
        trusted_input_baseline="a" * 40,
        task_name="queue-spsc",
        now=NOW,
        unique=UNIQUE,
    )

    store.state.create_run(manifest)

    assert store.state.load_run(manifest.run_id).task_name == "queue-spsc"
    raw = json.loads(store.state._run_manifest_path(manifest.run_id).read_text(encoding="utf-8"))
    assert raw["task_name"] == "queue-spsc"


def test_run_manifest_loads_without_optional_task_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)
    path = store.state._run_manifest_path(manifest.run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["task_name"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert store.state.load_run(manifest.run_id).task_name is None


@pytest.mark.parametrize("task_name", ["", "Uppercase", "../queue", "queue/spsc"])
def test_run_manifest_rejects_invalid_task_identity(tmp_path: Path, task_name: str) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValidationError, match="task_name"):
        store.state.new_run_manifest(
            "queue",
            branch="vibesys/queue",
            vibesys_version="0.2.0",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=run_execution_record(),
            orchestration=_descriptor(),
            trusted_input_baseline="a" * 40,
            task_name=task_name,
            now=NOW,
            unique=UNIQUE,
        )


@pytest.mark.parametrize("object_id", ["a" * 40, "b" * 64])
def test_run_manifest_accepts_git_sha1_and_sha256_object_ids(
    tmp_path: Path,
    object_id: str,
) -> None:
    store = _store(tmp_path)

    manifest = store.state.new_run_manifest(
        "queue",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=_descriptor(),
        trusted_input_baseline=object_id,
        now=NOW,
        unique=UNIQUE,
    )

    assert manifest.trusted_input_baseline == object_id


@pytest.mark.parametrize("object_id", ["a" * 39, "A" * 40, "b" * 63, "../baseline"])
def test_run_manifest_rejects_invalid_git_object_ids(
    tmp_path: Path,
    object_id: str,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValidationError, match="trusted_input_baseline"):
        store.state.new_run_manifest(
            "queue",
            branch="vibesys/queue",
            vibesys_version="0.2.0",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=run_execution_record(),
            orchestration=_descriptor(),
            trusted_input_baseline=object_id,
            now=NOW,
            unique=UNIQUE,
        )


def test_new_run_manifest_accepts_a_preallocated_safe_run_id(tmp_path: Path) -> None:
    store = _store(tmp_path)

    manifest = store.state.new_run_manifest(
        "queue",
        branch="vibesys/preallocated-run",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=_descriptor(),
        trusted_input_baseline="b" * 40,
        run_id="preallocated-run",
        now=NOW,
    )

    assert manifest.run_id == "preallocated-run"


def test_new_run_manifest_rejects_an_unsafe_preallocated_run_id(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ProjectStateError, match="Invalid VibeSys run ID"):
        store.state.new_run_manifest(
            "queue",
            branch="vibesys/queue",
            vibesys_version="0.2.0",
            run_environment=RunEnvironmentRecord(name="local"),
            execution=run_execution_record(),
            orchestration=_descriptor(),
            trusted_input_baseline="b" * 40,
            run_id="../escape",
            now=NOW,
        )


def test_create_run_rejects_a_manifest_for_another_project(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = store.state.new_run_manifest(
        "queue",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=_descriptor(),
        trusted_input_baseline="c" * 40,
        now=NOW,
        unique=UNIQUE,
    ).model_copy(update={"project_id": "another-project"})

    with pytest.raises(ProjectStateError, match="belongs to project"):
        store.state.create_run(manifest)


def test_current_and_latest_run_resolution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _run(store, minute=1)
    second = _run(store, minute=2)

    assert store.state.list_runs() == [first, second]
    assert store.state.latest_run() == second
    assert store.state.resolve_run() == second
    store.state.set_current_run(first.run_id)
    assert store.state.current_run_id() == first.run_id
    assert store.state.resolve_run() == first
    assert store.state.resolve_run(second.run_id) == second
    store.state.set_current_run(None)
    assert store.state.current_run_id() is None
    assert store.state.resolve_run() == second


def test_resolve_run_without_runs_is_actionable(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ProjectStateError, match=r"No VibeSys runs.*\.vibesys/state"):
        store.state.resolve_run()


@pytest.mark.parametrize("run_id", ["../escape", "/absolute", "Uppercase", "", "a/b"])
def test_run_id_validation_prevents_path_escape(tmp_path: Path, run_id: str) -> None:
    store = _store(tmp_path)

    with pytest.raises(ProjectStateError, match="Invalid VibeSys run ID"):
        store.state._run_manifest_path(run_id)


def test_containment_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    runs_dir = tmp_path / ".vibesys/state" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "escaped").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="escapes"):
        store.state._run_manifest_path("escaped")


def test_containment_rejects_in_tree_symlinked_run_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runs_dir = tmp_path / ".vibesys/state" / "runs"
    target = runs_dir / "target"
    target.mkdir(parents=True)
    (runs_dir / "alias").symlink_to(target, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="must not be a symlink"):
        store.state._run_manifest_path("alias")


@pytest.mark.parametrize(
    "namespace",
    ["", ".", "..", "../agent", "/agent", "agent/state", "agent\\state", "Agent"],
)
def test_state_namespace_validation_prevents_path_escape(
    tmp_path: Path,
    namespace: str,
) -> None:
    store = _store(tmp_path)
    run = _run(store)

    with pytest.raises(ProjectStateError, match="Invalid VibeSys state namespace"):
        store.state.portable_namespace(run.run_id, namespace)
    with pytest.raises(ProjectStateError, match="Invalid VibeSys state namespace"):
        store.state.local_namespace(run.run_id, namespace)


@pytest.mark.parametrize("local", [False, True])
def test_state_namespace_rejects_symlink_aliases(tmp_path: Path, *, local: bool) -> None:
    store = _store(tmp_path)
    run = _run(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = (
        store.state._local_dir / "runs" / run.run_id
        if local
        else tmp_path / ".vibesys/state" / "runs" / run.run_id
    )
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "unsafe").symlink_to(outside, target_is_directory=True)

    state_namespace = store.state.local_namespace if local else store.state.portable_namespace
    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        state_namespace(run.run_id, "unsafe")


def test_state_namespace_rejects_in_tree_symlink_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    parent = tmp_path / ".vibesys/state" / "runs" / run.run_id
    target = parent / "target"
    target.mkdir()
    (parent / "alias").symlink_to(target, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="must not be a symlink"):
        store.state.portable_namespace(run.run_id, "alias")


def test_state_namespace_rejects_existing_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)

    with pytest.raises(ProjectStateError, match="is not a directory"):
        store.state.portable_namespace(run.run_id, "run.json")


def test_worktrees_directory_rejects_symlink_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    worktrees = tmp_path / ".vibesys/state" / "local" / "runs" / run.run_id / "worktrees"
    worktrees.parent.mkdir(parents=True)
    worktrees.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        store.state._worktrees_dir(run.run_id)


def test_state_namespace_round_trips_strict_models_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    cursor = _Cursor(round=3, phase="judge")

    namespace.save("cursor.json", cursor)

    assert namespace.load("cursor.json", _Cursor) == cursor
    assert namespace.load_optional("cursor.json", _Cursor) == cursor
    raw_path = namespace.external_directory() / "cursor.json"
    assert json.loads(raw_path.read_text(encoding="utf-8")) == {
        "phase": "judge",
        "round": 3,
    }
    assert not list(raw_path.parent.glob("*.tmp"))


def test_state_namespace_prepares_and_applies_exact_typed_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.local_namespace(run.run_id, "agent")
    cursor = _Cursor(round=3, phase="judge")

    slot = namespace.slot("active.json", _Cursor)
    transition = slot.transition(cursor)
    serialized = slot.serialize_transition(transition)
    restored = slot.deserialize_transition(serialized)
    assert namespace.load_optional("active.json", _Cursor) is None

    slot.apply(restored)

    assert namespace.load("active.json", _Cursor) == cursor

    deletion = slot.deserialize_transition(slot.serialize_transition(slot.transition(None)))
    slot.apply(deletion)
    assert namespace.load_optional("active.json", _Cursor) is None


def test_typed_state_slot_snapshots_exact_replacement(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    slot = namespace.slot("state.json", _Cursor)
    transition = slot.transition(_Cursor(round=3, phase="judge"))

    snapshot = slot.snapshot_transition(transition)

    assert snapshot == StateSnapshot._create(
        namespace_root=PurePosixPath(f".vibesys/state/runs/{run.run_id}/agent"),
        files=(
            StateFile(
                relative_path=PurePosixPath("state.json"),
                contents=b'{\n  "phase": "judge",\n  "round": 3\n}\n',
            ),
        ),
    )


def test_typed_state_slot_cannot_snapshot_deletion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    slot = store.state.portable_namespace(run.run_id, "agent").slot(
        "state.json",
        _Cursor,
    )

    with pytest.raises(ProjectStateError, match="deletion transition"):
        slot.snapshot_transition(slot.transition(None))


def test_state_namespace_rejects_transition_for_another_namespace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    agent = store.state.local_namespace(run.run_id, "agent")
    plain = store.state.local_namespace(run.run_id, "plain")
    transition = plain.transition("cursor.json", _Cursor(round=1, phase="judge"))

    with pytest.raises(ProjectStateError, match="outside this namespace"):
        agent.apply(transition)

    assert plain.load_optional("cursor.json", _Cursor) is None


def test_typed_state_slot_rejects_schema_invalid_reconstructed_document(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    slot = store.state.local_namespace(run.run_id, "plain").slot("cursor.json", _Cursor)
    payload = b'{"schema_version":1,"document":{"round":1,"unexpected":true}}'

    with pytest.raises(ProjectStateError, match="does not match the slot schema"):
        slot.deserialize_transition(payload)

    assert slot.load_optional() is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"schema_version":2,"document":null}',
        b'{"schema_version":1,"document":[]}',
    ],
)
def test_typed_state_slot_rejects_malformed_serialized_transition(
    tmp_path: Path,
    payload: bytes,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    slot = store.state.local_namespace(run.run_id, "plain").slot("cursor.json", _Cursor)

    with pytest.raises(ProjectStateError, match="transition"):
        slot.deserialize_transition(payload)


def test_state_namespace_distinguishes_missing_from_corrupt_optional_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")

    assert namespace.load_optional("cursor.json", _Cursor) is None
    with pytest.raises(StateModelNotFoundError, match=r"state model does not exist.*cursor\.json"):
        namespace.load("cursor.json", _Cursor)

    path = namespace.external_directory() / "cursor.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ProjectStateError, match=r"Invalid VibeSys state model.*cursor\.json"):
        namespace.load_optional("cursor.json", _Cursor)


def test_state_namespace_rejects_unknown_model_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    path = namespace.external_directory() / "cursor.json"
    path.write_text(
        json.dumps({"round": 1, "phase": "judge", "surprise": True}),
        encoding="utf-8",
    )

    with pytest.raises(
        ProjectStateError,
        match=r"Invalid VibeSys state model.*cursor\.json.*surprise",
    ):
        namespace.load("cursor.json", _Cursor)


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        ".",
        "../cursor.json",
        "/cursor.json",
        "nested/../cursor.json",
        "a//b.json",
        "a\\b.json",
    ],
)
def test_state_namespace_rejects_unsafe_relative_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")

    with pytest.raises(ProjectStateError, match=r"safe portable|non-empty portable"):
        namespace.save(relative_path, _Cursor(round=1, phase="judge"))


def test_state_namespace_rejects_symlinks_below_namespace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    root = namespace.external_directory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="must not contain symlinks"):
        namespace.snapshot()
    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        namespace.save("nested/cursor.json", _Cursor(round=1, phase="judge"))

    assert list(outside.iterdir()) == []


def test_state_namespace_revalidates_its_root_before_every_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    root = tmp_path / ".vibesys/state" / "runs" / run.run_id / "plain"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        namespace.snapshot()


def test_state_namespace_delete_reports_presence_and_rejects_directories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.local_namespace(run.run_id, "agent")
    namespace.save("active.json", _Cursor(round=1, phase="implementer"))

    assert namespace.delete("active.json") is True
    assert namespace.delete("active.json") is False
    namespace.external_directory("nested")
    with pytest.raises(ProjectStateError, match="not a file"):
        namespace.delete("nested")


def test_portable_state_snapshot_is_deterministic_and_namespace_relative(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "evolve")
    namespace.save("z.json", _Cursor(round=2, phase="profile"))
    namespace.save("nested/a.json", _Cursor(round=1, phase="judge"))

    root = namespace.external_directory()
    expected = StateSnapshot._create(
        namespace_root=PurePosixPath(f".vibesys/state/runs/{run.run_id}/evolve"),
        files=(
            StateFile(
                relative_path=PurePosixPath("nested/a.json"),
                contents=(root / "nested/a.json").read_bytes(),
            ),
            StateFile(
                relative_path=PurePosixPath("z.json"),
                contents=(root / "z.json").read_bytes(),
            ),
        ),
    )

    assert namespace.snapshot() == expected
    assert namespace.snapshot() == namespace.snapshot()


def test_empty_portable_namespace_has_an_empty_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)

    snapshot = store.state.portable_namespace(run.run_id, "runtime").snapshot()

    assert snapshot._namespace_root == PurePosixPath(f".vibesys/state/runs/{run.run_id}/runtime")
    assert snapshot.files == ()


def test_namespace_byte_file_preserves_exact_legacy_contents(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    contents = b'{"round":1}\n'

    assert namespace.read_bytes("rounds/0001.json") is None
    prepared = namespace.snapshot_bytes("rounds/0001.json", contents)
    namespace.write_bytes("rounds/0001.json", contents)

    assert namespace.read_bytes("rounds/0001.json") == contents
    assert namespace.entries("rounds") == ("0001.json",)
    assert prepared.files == (
        StateFile(relative_path=PurePosixPath("rounds/0001.json"), contents=contents),
    )
    with pytest.raises(ProjectStateError):
        namespace.snapshot_bytes("../outside.json", contents)
    with pytest.raises(ProjectStateError, match="cannot be snapshotted"):
        store.state.local_namespace(run.run_id, "agent").snapshot_bytes("active.json", contents)
    with pytest.raises(TypeError, match="must be bytes"):
        namespace.snapshot_bytes("rounds/0002.json", cast("bytes", "text"))


def test_initialization_snapshot_contains_only_selected_run_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _run(store)
    second = _run(store, minute=1)

    snapshot = store.state.initialization_snapshot(first.run_id)

    assert snapshot._namespace_root == PurePosixPath(".vibesys/state")
    assert tuple(file.relative_path for file in snapshot.files) == (
        PurePosixPath(".gitignore"),
        PurePosixPath("project.json"),
        PurePosixPath(f"runs/{first.run_id}/run.json"),
    )
    assert PurePosixPath(f"runs/{second.run_id}/run.json") not in {
        file.relative_path for file in snapshot.files
    }
    assert snapshot.files[0].contents == store.state._metadata_gitignore_path.read_bytes()
    assert snapshot.files[1].contents == store.state._project_manifest_path.read_bytes()
    assert snapshot.files[2].contents == store.state._run_manifest_path(first.run_id).read_bytes()


def test_run_manifest_snapshot_is_rooted_at_the_selected_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)

    snapshot = store.state.run_manifest_snapshot(run.run_id)

    assert snapshot == StateSnapshot._create(
        namespace_root=PurePosixPath(f".vibesys/state/runs/{run.run_id}"),
        files=(
            StateFile(
                relative_path=PurePosixPath("run.json"),
                contents=store.state._run_manifest_path(run.run_id).read_bytes(),
            ),
        ),
    )


def test_metadata_snapshot_rejects_symlinked_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    outside = tmp_path / "outside"
    outside.write_text("local\n", encoding="utf-8")
    store.state._metadata_gitignore_path.unlink()
    store.state._metadata_gitignore_path.symlink_to(outside)

    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        store.state.initialization_snapshot(run.run_id)


@pytest.mark.parametrize(
    "root",
    [
        PurePosixPath("elsewhere"),
        PurePosixPath(".vibesys/state/local"),
        PurePosixPath(".vibesys/state/runs"),
        PurePosixPath(".vibesys/state/runs/Uppercase"),
        PurePosixPath(".vibesys/state/runs/run-1/Uppercase"),
    ],
)
def test_state_snapshot_rejects_unsafe_or_local_roots(root: PurePosixPath) -> None:
    with pytest.raises(ValueError, match=r"portable state snapshot|invalid"):
        StateSnapshot._create(namespace_root=root, files=())


@pytest.mark.parametrize(
    "relative_path",
    [PurePosixPath("../secret"), PurePosixPath("/absolute"), PurePosixPath(".")],
)
def test_state_file_rejects_unsafe_relative_paths(relative_path: PurePosixPath) -> None:
    with pytest.raises(ValueError, match="portable relative path"):
        StateFile(relative_path=relative_path, contents=b"secret")


def test_state_snapshot_rejects_local_file_below_metadata_root() -> None:
    local_file = StateFile(
        relative_path=PurePosixPath("local/runs/run-1/state.json"),
        contents=b"{}",
    )

    with pytest.raises(ValueError, match=r"must not contain \.vibesys/state/local"):
        StateSnapshot._create(namespace_root=PurePosixPath(".vibesys/state"), files=(local_file,))


def test_machine_local_state_namespace_cannot_be_snapshotted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.local_namespace(run.run_id, "agent")
    namespace.save("active.json", _Cursor(round=1, phase="implementer"))

    with pytest.raises(ProjectStateError, match=r"Machine-local.*cannot be snapshotted"):
        namespace.snapshot()


def test_git_paths_resolve_portable_snapshot_without_layout_work_by_consumer(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "evolve")
    namespace.save("population.json", _Cursor(round=2, phase="complete"))

    plan = store.state.git_integration(run.run_id).resolve_replacement_snapshot(
        namespace.snapshot()
    )

    assert plan.scope_pathspec == f".vibesys/state/runs/{run.run_id}/evolve"
    assert plan.destination_root == tmp_path / ".vibesys/state" / "runs" / run.run_id / "evolve"
    assert len(plan.files) == 1
    assert plan.files[0].pathspec == f".vibesys/state/runs/{run.run_id}/evolve/population.json"
    assert plan.files[0].destination == plan.destination_root / "population.json"
    assert plan.contains_pathspec(plan.files[0].pathspec)
    assert not plan.contains_pathspec("candidate.py")


def test_git_paths_reject_snapshot_from_another_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _run(store)
    second = _run(store, minute=1)
    snapshot = store.state.portable_namespace(second.run_id, "evolve").snapshot()

    with pytest.raises(ValueError, match="belongs to run"):
        store.state.git_integration(first.run_id).resolve_snapshot(snapshot)


def test_git_paths_validate_candidate_worktrees_without_symlink_traversal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    capability = store.state.git_integration(run.run_id)
    worktrees = store.state._worktrees_dir(run.run_id)

    assert capability.validate_candidate_worktree(worktrees / "candidate") == (
        worktrees / "candidate"
    )
    with pytest.raises(ValueError, match="must be below"):
        capability.validate_candidate_worktree(tmp_path / "candidate")
    with pytest.raises(ValueError, match="must be below"):
        capability.validate_candidate_worktree(worktrees / "candidate" / "..")

    outside = tmp_path.parent / "outside-worktree"
    outside.mkdir(exist_ok=True)
    worktrees.mkdir(parents=True)
    (worktrees / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="must be below"):
        capability.validate_candidate_worktree(worktrees / "linked" / "candidate")
