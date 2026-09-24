# Package-boundary tests intentionally inspect private on-disk details.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from vs_loop_state.api import RoundRecord, parse_round_record, serialize_round_record
from vs_project.api import (
    PROJECT_SCHEMA_VERSION,
    RUN_SCHEMA_VERSION,
    AgentRunConfiguration,
    EvolveRunConfiguration,
    PlainRunConfiguration,
    Project,
    ProjectManifest,
    ProjectStateError,
    RunConfiguration,
    RunEnvironmentRecord,
    RunManifest,
    RunResourceRequest,
    RunSchemaMigrationRequiredError,
    StateFile,
    StateModelNotFoundError,
    StateSnapshot,
    compare_resume_configurations,
    generate_run_id,
    is_project_state_path,
    serialize_round,
)

NOW = datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC)
UNIQUE = UUID("12345678-1234-5678-1234-567812345678")
RUN_CONFIGURATION_ADAPTER = TypeAdapter(RunConfiguration)


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: int
    phase: str


def _configuration() -> AgentRunConfiguration:
    return AgentRunConfiguration(
        model="gpt-5",
        outer_loop="agent",
        run_environment=RunEnvironmentRecord(name="local"),
        inner_loop="multi-agent",
        interface="inprocess",
        agent_backend="cli",
        agent_driver="agentshim",
        cli_provider="codex",
        cli_timeout=1800,
        compute_backend="cpu",
        profiler="linux-cpu",
        max_rounds=10,
        max_retries_per_round=3,
        judge_every=3,
        official_eval_every=3,
        memory_layout="files",
        modality="text_generation",
        default_reasoning_effort="high",
        outer_model="gpt-5.6-sol",
        outer_reasoning_effort="xhigh",
        inner_model="gpt-5.6-luna",
        inner_reasoning_effort="medium",
        operator_constraints=("Do not change the ABI",),
    )


def _plain_configuration() -> PlainRunConfiguration:
    return PlainRunConfiguration(
        model="gpt-5",
        outer_loop="plain",
        run_environment=RunEnvironmentRecord(name="local"),
        agent_backend="cli",
        cli_provider="codex",
        cli_timeout=1800,
        compute_backend="cpu",
        profiler="none",
        max_rounds=5,
        max_attempts_per_issue=3,
        max_issues_per_perf_eval=3,
    )


def _evolve_configuration() -> EvolveRunConfiguration:
    return EvolveRunConfiguration(
        model="gpt-5",
        outer_loop="evolve",
        run_environment=RunEnvironmentRecord(name="local"),
        agent_backend="cli",
        cli_provider="codex",
        cli_timeout=1800,
        compute_backend="cpu",
        profiler="none",
        modality="text_generation",
        max_generations=8,
        children_per_generation=2,
        k_top_inspirations=2,
        k_random_inspirations=2,
        selection_temperature=0.5,
        seed=17,
        search_policy="openevolve",
        openevolve_population_size=100,
        openevolve_archive_size=20,
        openevolve_num_islands=5,
        openevolve_migration_interval=50,
        openevolve_migration_rate=0.1,
        frontier_bias=0.7,
        bootstrap_max_attempts=5,
        keep_deployments=False,
        max_parallelism=1,
        objectives=("throughput:max", "memory:min"),
    )


def _store(tmp_path: Path) -> Project:
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    store = Project.open(tmp_path)
    store.state.create_project("Queue SPSC", now=NOW)
    return store


def _local_state_dir(store: Project, run_id: str = "path-probe") -> Path:
    """Locate machine-local state through the public log-directory API."""
    return store.state.log_directory(run_id).parents[2]


def _metadata_dir(store: Project) -> Path:
    """Return the stable, project-relative metadata directory."""
    return store.state.project_root / ".vibesys/state"


def _run_manifest_path(store: Project, run_id: str) -> Path:
    """Return the documented portable run-manifest location."""
    return store.state.project_root / ".vibesys/state/runs" / run_id / "run.json"


def _rounds_dir(store: Project, run_id: str) -> Path:
    """Return the documented portable completed-round directory."""
    return store.state.project_root / ".vibesys/state/runs" / run_id / "agent/rounds"


def _worktrees_dir(store: Project, run_id: str) -> Path:
    """Locate reserved worktrees through the public candidate path API."""
    return store.state.candidate_worktree_directory(run_id, "probe").parents[1]


def _round_transaction_path(store: Project, run_id: str) -> Path:
    """Return the transaction file beside public per-run logs."""
    return _local_state_dir(store, run_id) / "runs" / run_id / "round-transaction.json"


def _run(store: Project, *, minute: int = 0) -> RunManifest:
    created_at = NOW + timedelta(minutes=minute)
    manifest = store.state.new_run_manifest(
        "Queue SPSC",
        branch=f"vibesys/queue-{minute}",
        vibesys_version="0.2.0",
        configuration=_configuration(),
        trusted_input_baseline="a" * 40,
        now=created_at,
        unique=UUID(int=minute + 1),
    )
    store.state.create_run(manifest)
    return manifest


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
        RunManifest(
            schema_version=RUN_SCHEMA_VERSION,
            run_id="run-1",
            project_id="queue-abc",
            display_name="Queue",
            created_at=NOW,
            input_fingerprint="a" * 64,
            trusted_input_baseline="b" * 40,
            branch="vibesys/run-1",
            vibesys_version="0.2.0",
            configuration=_configuration(),
            **forbidden_field,
        )


def test_run_configuration_rejects_secret_and_machine_local_fields() -> None:
    raw = _configuration().model_dump()
    raw["environment"] = {"TOKEN": "not persisted"}

    with pytest.raises(ValidationError, match="environment"):
        RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_rounds", 0),
        ("max_retries_per_round", 0),
        ("judge_every", 0),
        ("official_eval_every", 0),
        ("cli_timeout", 0),
        ("max_retries_per_round", "3"),
    ],
)
def test_run_configuration_strictly_validates_positive_counts(
    field: str,
    value: object,
) -> None:
    raw = _configuration().model_dump()
    raw[field] = value

    with pytest.raises(ValidationError, match=field):
        RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)


@pytest.mark.parametrize("field", ["inner_loop", "interface", "max_retries_per_round"])
def test_run_configuration_requires_core_agent_loop_behavior(field: str) -> None:
    raw = _configuration().model_dump()
    del raw[field]

    with pytest.raises(ValidationError, match=field):
        RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)


def test_run_configuration_allows_optional_behavior_overrides_to_be_absent() -> None:
    raw = _configuration().model_dump()
    for field in (
        "model",
        "agent_driver",
        "cli_provider",
        "cli_timeout",
        "profiler",
        "modality",
        "default_reasoning_effort",
        "outer_model",
        "outer_reasoning_effort",
        "inner_model",
        "inner_reasoning_effort",
    ):
        raw[field] = None

    configuration = RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)

    assert configuration.cli_timeout is None
    assert configuration.modality is None
    assert configuration.outer_model is None
    assert configuration.inner_reasoning_effort is None


def test_run_configuration_is_frozen() -> None:
    configuration = _configuration()
    frozen_field = "inner_loop"

    with pytest.raises(ValidationError, match="frozen"):
        setattr(configuration, frozen_field, "single-agent")


@pytest.mark.parametrize(
    ("configuration", "expected_type"),
    [
        (_configuration(), AgentRunConfiguration),
        (_plain_configuration(), PlainRunConfiguration),
        (_evolve_configuration(), EvolveRunConfiguration),
    ],
)
def test_run_configuration_discriminates_outer_loop(
    configuration: RunConfiguration,
    expected_type: type[RunConfiguration],
) -> None:
    parsed = RUN_CONFIGURATION_ADAPTER.validate_python(
        configuration.model_dump(),
        strict=True,
    )

    assert type(parsed) is expected_type
    with pytest.raises(ValidationError, match="frozen"):
        parsed.agent_backend = "stub"


def test_profile_guided_run_configuration_round_trips_as_agent_configuration() -> None:
    payload = _configuration().model_dump()
    payload["outer_loop"] = "profile-guided"

    parsed = RUN_CONFIGURATION_ADAPTER.validate_python(
        payload,
        strict=True,
    )

    assert type(parsed) is AgentRunConfiguration
    assert parsed.model_dump() == payload


def test_run_configurations_declare_resume_limits() -> None:
    agent = compare_resume_configurations(_configuration(), _configuration())
    plain = compare_resume_configurations(_plain_configuration(), _plain_configuration())
    evolve = compare_resume_configurations(_evolve_configuration(), _evolve_configuration())

    assert (agent.limit_field, agent.recorded_limit) == ("max_rounds", 10)
    assert (plain.limit_field, plain.recorded_limit) == ("max_rounds", 5)
    assert (evolve.limit_field, evolve.recorded_limit) == ("max_generations", 8)


def test_resume_comparison_requires_matching_outer_loops() -> None:
    with pytest.raises(ValueError, match="outer loops must match"):
        compare_resume_configurations(_configuration(), _plain_configuration())


def test_agent_resume_comparison_adopts_only_omitted_legacy_objectives() -> None:
    requested = _configuration().model_copy(update={"objectives": ("throughput:max",)})
    legacy_payload = requested.model_dump(exclude={"objectives"})
    recorded = AgentRunConfiguration.model_validate(legacy_payload)

    comparison = compare_resume_configurations(recorded, requested)
    assert comparison.migration_required
    assert comparison.changed_fields == ()

    explicit = recorded.model_copy(update={"objectives": ()})
    comparison = compare_resume_configurations(explicit, requested)
    assert not comparison.migration_required
    assert comparison.changed_fields == ("objectives",)


@pytest.mark.parametrize("outer_loop", [None, "unknown"])
def test_run_configuration_requires_known_outer_loop(outer_loop: str | None) -> None:
    raw = _configuration().model_dump()
    if outer_loop is None:
        del raw["outer_loop"]
    else:
        raw["outer_loop"] = outer_loop

    with pytest.raises(ValidationError, match="outer_loop"):
        RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)


def test_run_configuration_rejects_fields_from_another_loop() -> None:
    raw = _plain_configuration().model_dump()
    raw["inner_loop"] = "multi-agent"

    with pytest.raises(ValidationError, match="inner_loop"):
        RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)


@pytest.mark.parametrize(
    ("configuration", "field", "value"),
    [
        (_plain_configuration(), "max_attempts_per_issue", 0),
        (_plain_configuration(), "max_issues_per_perf_eval", "3"),
        (_evolve_configuration(), "max_generations", 0),
        (_evolve_configuration(), "k_top_inspirations", -1),
        (_evolve_configuration(), "selection_temperature", 0.0),
        (_evolve_configuration(), "selection_temperature", float("inf")),
        (_evolve_configuration(), "openevolve_migration_rate", 1.1),
        (_evolve_configuration(), "frontier_bias", -0.1),
        (_evolve_configuration(), "max_parallelism", 0),
    ],
)
def test_loop_specific_configuration_constraints(
    configuration: RunConfiguration,
    field: str,
    value: object,
) -> None:
    raw = configuration.model_dump()
    raw[field] = value

    with pytest.raises(ValidationError, match=field):
        RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)


def test_evolve_configuration_rejects_openevolve_settings_for_vibesys_policy() -> None:
    raw = _evolve_configuration().model_dump()
    raw["search_policy"] = "vibesys"

    with pytest.raises(ValidationError, match="OpenEvolve settings"):
        RUN_CONFIGURATION_ADAPTER.validate_python(raw, strict=True)


def test_create_project_writes_portable_committed_manifest(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "queue.rs").write_text("pub struct Queue;\n", encoding="utf-8")
    store = Project.open(tmp_path)

    manifest = store.state.create_project("Queue SPSC", now=NOW)

    assert store.state.load_project() == manifest
    assert (_metadata_dir(store) / ".gitignore").read_text(encoding="utf-8") == "/local/\n"
    raw = json.loads((_metadata_dir(store) / "project.json").read_text(encoding="utf-8"))
    assert raw == {
        "created_at": "2026-08-11T12:34:56Z",
        "initial_input_fingerprint": manifest.initial_input_fingerprint,
        "project_id": manifest.project_id,
        "schema_version": PROJECT_SCHEMA_VERSION,
    }
    serialized = (_metadata_dir(store) / "project.json").read_text(encoding="utf-8")
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
    assert (_metadata_dir(store) / ".gitignore").read_text(encoding="utf-8") == "/local/\n"


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
    ("relative_path", "ownership"),
    [
        ("src/queue.py", "not-owned"),
        (".git/HEAD", "not-owned"),
        (".vs/project.json", "not-owned"),
        (".vibesys/tasks/queue/vibesys.input.toml", "not-owned"),
        ("nested/.vibesys/tasks/queue/OBJECTIVE.md", "not-owned"),
        (".vibesys/stateful/project.json", "not-owned"),
        ("nested/.vibesys/state/project.json", "owned"),
        ("agent.toml", "not-owned"),
        ("nested/.env.local", "not-owned"),
    ],
)
def test_project_state_path_ownership_is_semantic(
    relative_path: str,
    ownership: str,
) -> None:
    assert is_project_state_path(relative_path) is (ownership == "owned")


def test_semantic_runtime_and_sandbox_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    run_id = "run-1"

    early_log_directory = Project.log_directory_for(project, run_id)
    store = _store(project)
    run = _run(store)

    assert early_log_directory == store.state.log_directory(run_id)
    assert store.state.log_directory(run.run_id).is_dir()
    assert store.state.log_directory(run.run_id).is_relative_to(_local_state_dir(store))
    assert store.state.model_cache_directory("huggingface").is_relative_to(_local_state_dir(store))
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

    local_state_dir = Project.log_directory_for(project, "run-1").parents[2]
    assert (local_state_dir / "current-run").read_bytes() == b"run-1\n"
    assert (local_state_dir / "runs/run-1/logs/run-events.jsonl").read_bytes() == (
        b'{"type":"server_started"}\n'
    )
    assert (local_state_dir / "runs/run-1/agent/active.json").read_bytes() == (
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
    _local_state_dir(store).mkdir(parents=True)
    outside.mkdir()
    (_local_state_dir(store) / "cache").symlink_to(outside, target_is_directory=True)

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
    (
        store.state.portable_namespace(run.run_id, "agent").external_directory() / "linked"
    ).symlink_to(
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

    assert (_metadata_dir(store) / ".gitignore").read_text(encoding="utf-8") == (
        "custom.tmp\n/local/\n"
    )


def test_create_project_rejects_symlinked_metadata_root_before_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    store = Project.open(project)
    (store.state.project_root / ".vibesys").mkdir()
    _metadata_dir(store).symlink_to(outside, target_is_directory=True)

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
    (store.state.project_root / ".vibesys").symlink_to(outside, target_is_directory=True)

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
        configuration=_configuration(),
        trusted_input_baseline="a" * 40,
        now=NOW,
        unique=UNIQUE,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    _local_state_dir(store).parent.mkdir(parents=True, exist_ok=True)
    _local_state_dir(store).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="local metadata root must not be a symlink"):
        store.state.create_run(manifest)

    assert not (_metadata_dir(store) / "runs").exists()
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
    assert _run_manifest_path(store, manifest.run_id) == (
        tmp_path / ".vibesys/state" / "runs" / manifest.run_id / "run.json"
    )
    assert store.state.log_directory(manifest.run_id) == (
        _local_state_dir(store) / "runs" / manifest.run_id / "logs"
    )
    assert _rounds_dir(store, manifest.run_id) == (
        tmp_path / ".vibesys/state" / "runs" / manifest.run_id / "agent" / "rounds"
    )
    with pytest.raises(ProjectStateError, match="not agent-visible"):
        store.state.local_namespace(manifest.run_id, "agent").agent_visible_path("active.json")
    assert _round_transaction_path(store, manifest.run_id) == (
        _local_state_dir(store) / "runs" / manifest.run_id / "round-transaction.json"
    )
    assert _worktrees_dir(store, manifest.run_id) == (
        tmp_path / ".vibesys/state" / "local" / "runs" / manifest.run_id / "worktrees"
    )
    assert store.state.log_directory(manifest.run_id).is_dir()
    assert not (tmp_path / ".vibesys/state" / "runs" / manifest.run_id / "agent").exists()
    assert not (_local_state_dir(store) / "runs" / manifest.run_id / "agent").exists()
    assert not _worktrees_dir(store, manifest.run_id).exists()
    committed = _run_manifest_path(store, manifest.run_id).read_text(encoding="utf-8")
    assert str(tmp_path) not in committed
    assert "token" not in committed


def test_run_manifest_persists_complete_agent_loop_behavior(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)

    raw = json.loads(_run_manifest_path(store, manifest.run_id).read_text(encoding="utf-8"))

    assert raw["trusted_input_baseline"] == "a" * 40
    assert raw["configuration"] == {
        "agent_backend": "cli",
        "agent_driver": "agentshim",
        "cli_provider": "codex",
        "cli_timeout": 1800,
        "compute_backend": "cpu",
        "default_reasoning_effort": "high",
        "inner_loop": "multi-agent",
        "inner_model": "gpt-5.6-luna",
        "inner_reasoning_effort": "medium",
        "interface": "inprocess",
        "judge_every": 3,
        "max_retries_per_round": 3,
        "max_rounds": 10,
        "memory_layout": "files",
        "modality": "text_generation",
        "model": "gpt-5",
        "official_eval_every": 3,
        "objectives": [],
        "operator_constraints": ["Do not change the ABI"],
        "outer_loop": "agent",
        "outer_model": "gpt-5.6-sol",
        "outer_reasoning_effort": "xhigh",
        "profiler": "linux-cpu",
        "run_environment": {
            "app": None,
            "gpu": None,
            "image": None,
            "model_volume": None,
            "name": "local",
            "resources": None,
        },
    }


@pytest.mark.parametrize(
    ("configuration", "expected_type"),
    [
        (_configuration(), AgentRunConfiguration),
        (_plain_configuration(), PlainRunConfiguration),
        (_evolve_configuration(), EvolveRunConfiguration),
    ],
)
def test_run_manifest_round_trips_each_outer_loop_configuration(
    tmp_path: Path,
    configuration: RunConfiguration,
    expected_type: type[RunConfiguration],
) -> None:
    store = _store(tmp_path)
    manifest = store.state.new_run_manifest(
        "queue",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        configuration=configuration,
        trusted_input_baseline="a" * 40,
        now=NOW,
        unique=UNIQUE,
    )

    store.state.create_run(manifest)
    loaded = store.state.load_run(manifest.run_id)

    assert type(loaded.configuration) is expected_type
    assert loaded.configuration == configuration


def test_run_manifest_round_trips_optional_task_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = store.state.new_run_manifest(
        "queue",
        branch="vibesys/queue",
        vibesys_version="0.2.0",
        configuration=_configuration(),
        trusted_input_baseline="a" * 40,
        task_name="queue-spsc",
        now=NOW,
        unique=UNIQUE,
    )

    store.state.create_run(manifest)

    assert store.state.load_run(manifest.run_id).task_name == "queue-spsc"
    raw = json.loads(_run_manifest_path(store, manifest.run_id).read_text(encoding="utf-8"))
    assert raw["task_name"] == "queue-spsc"


def test_run_manifest_loads_legacy_state_without_task_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)
    path = _run_manifest_path(store, manifest.run_id)
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
            configuration=_configuration(),
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
        configuration=_configuration(),
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
            configuration=_configuration(),
            trusted_input_baseline=object_id,
            now=NOW,
            unique=UNIQUE,
        )


def test_update_run_configuration_preserves_manifest_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)
    updated_configuration = manifest.configuration.model_copy(update={"max_rounds": 20})

    result = store.state.update_run_configuration(manifest.run_id, updated_configuration)
    updated = store.state.load_run(manifest.run_id)

    assert result is None
    assert updated.configuration == updated_configuration
    assert updated.model_copy(update={"configuration": manifest.configuration}) == manifest


def test_update_run_configuration_rejects_outer_loop_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)

    with pytest.raises(ProjectStateError, match="uses outer loop 'agent', not 'plain'"):
        store.state.update_run_configuration(manifest.run_id, _plain_configuration())

    assert store.state.load_run(manifest.run_id) == manifest


def test_new_run_manifest_accepts_a_preallocated_safe_run_id(tmp_path: Path) -> None:
    store = _store(tmp_path)

    manifest = store.state.new_run_manifest(
        "queue",
        branch="vibesys/preallocated-run",
        vibesys_version="0.2.0",
        configuration=_configuration(),
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
            configuration=_configuration(),
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
        configuration=_configuration(),
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
        store.state.load_run(run_id)


def test_containment_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    runs_dir = tmp_path / ".vibesys/state" / "runs"
    runs_dir.mkdir(parents=True)
    (runs_dir / "escaped").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="escapes"):
        store.state.load_run("escaped")


def test_containment_rejects_in_tree_symlinked_run_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    runs_dir = tmp_path / ".vibesys/state" / "runs"
    target = runs_dir / "target"
    target.mkdir(parents=True)
    (runs_dir / "alias").symlink_to(target, target_is_directory=True)

    with pytest.raises(ProjectStateError, match="must not be a symlink"):
        store.state.load_run("alias")


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
        _local_state_dir(store) / "runs" / run.run_id
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
        _worktrees_dir(store, run.run_id)


def test_completed_round_directory_rejects_symlink_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    agent_dir = tmp_path / ".vibesys/state" / "runs" / run.run_id / "agent"
    agent_dir.mkdir()
    (agent_dir / "rounds").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectStateError, match=r"(?:escapes|must not be a symlink)"):
        store.state.save_round(
            run.run_id,
            RoundRecord(1, "a" * 40, 10.0, "ops/s", passed=True),
        )

    assert list(outside.iterdir()) == []


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

    assert snapshot.files == (
        StateFile(
            relative_path=PurePosixPath("state.json"),
            contents=b'{\n  "phase": "judge",\n  "round": 3\n}\n',
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
    expected_files = (
        StateFile(
            relative_path=PurePosixPath("nested/a.json"),
            contents=(root / "nested/a.json").read_bytes(),
        ),
        StateFile(
            relative_path=PurePosixPath("z.json"),
            contents=(root / "z.json").read_bytes(),
        ),
    )

    assert namespace.snapshot().files == expected_files
    assert namespace.snapshot() == namespace.snapshot()


def test_empty_portable_namespace_has_an_empty_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)

    snapshot = store.state.portable_namespace(run.run_id, "runtime").snapshot()

    assert snapshot.files == ()


def test_initialization_snapshot_contains_only_selected_run_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _run(store)
    second = _run(store, minute=1)

    snapshot = store.state.initialization_snapshot(first.run_id)

    assert tuple(file.relative_path for file in snapshot.files) == (
        PurePosixPath(".gitignore"),
        PurePosixPath("project.json"),
        PurePosixPath(f"runs/{first.run_id}/run.json"),
    )
    assert PurePosixPath(f"runs/{second.run_id}/run.json") not in {
        file.relative_path for file in snapshot.files
    }
    assert snapshot.files[0].contents == (_metadata_dir(store) / ".gitignore").read_bytes()
    assert snapshot.files[1].contents == (_metadata_dir(store) / "project.json").read_bytes()
    assert snapshot.files[2].contents == _run_manifest_path(store, first.run_id).read_bytes()


def test_run_manifest_snapshot_is_rooted_at_the_selected_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)

    snapshot = store.state.run_manifest_snapshot(run.run_id)

    assert snapshot.files == (
        StateFile(
            relative_path=PurePosixPath("run.json"),
            contents=_run_manifest_path(store, run.run_id).read_bytes(),
        ),
    )


def test_completed_round_snapshot_contains_one_canonical_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    first = RoundRecord(1, "a" * 40, 10.0, "ops/s", passed=True)
    second = RoundRecord(2, "b" * 40, 20.0, "ops/s", passed=True)
    store.state.save_round(run.run_id, first)
    second_snapshot = store.state.save_round(run.run_id, second)

    snapshot = store.state.completed_round_snapshot(run.run_id, 2)

    assert snapshot.files == (
        StateFile(
            relative_path=PurePosixPath("rounds/0002.json"),
            contents=second_snapshot.files[0].contents,
        ),
    )
    assert snapshot.files[0].contents == serialize_round(second)


@pytest.mark.parametrize("round_number", [0, 1, 2])
def test_completed_round_snapshot_rejects_missing_or_invalid_round(
    tmp_path: Path,
    round_number: int,
) -> None:
    store = _store(tmp_path)
    run = _run(store)

    with pytest.raises(ProjectStateError, match=r"positive|does not exist"):
        store.state.completed_round_snapshot(run.run_id, round_number)


def test_metadata_snapshot_rejects_symlinked_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    outside = tmp_path / "outside"
    outside.write_text("local\n", encoding="utf-8")
    (_metadata_dir(store) / ".gitignore").unlink()
    (_metadata_dir(store) / ".gitignore").symlink_to(outside)

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
        StateSnapshot._create(namespace_root=root, files=())  # noqa: SLF001  # lint-waiver: LW-006000 [SLF001]; exercise unsafe-root rejection through the opaque snapshot factory, which owns this validation.


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
        StateSnapshot._create(  # noqa: SLF001  # lint-waiver: LW-006001 [SLF001]; exercise portable-snapshot rejection for local paths through the opaque factory that owns this boundary.
            namespace_root=PurePosixPath(".vibesys/state"), files=(local_file,)
        )


def test_machine_local_state_namespace_cannot_be_snapshotted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.local_namespace(run.run_id, "agent")
    namespace.save("active.json", _Cursor(round=1, phase="implementer"))

    with pytest.raises(ProjectStateError, match=r"Machine-local.*cannot be snapshotted"):
        namespace.snapshot()


def test_round_record_serializer_is_public_and_round_trips() -> None:
    record = RoundRecord(
        round_number=7,
        commit="a" * 40,
        perf_metric=123.0,
        perf_unit="ops/s",
        passed=True,
        official_evaluation=True,
    )

    payload = serialize_round_record(record)

    assert payload["round"] == 7
    assert "round_number" not in payload
    assert parse_round_record(payload) == record


def test_project_state_serializes_validated_canonical_round_bytes() -> None:
    record = RoundRecord(
        round_number=7,
        commit="a" * 40,
        perf_metric=123.0,
        perf_unit="ops/s",
        passed=True,
        official_evaluation=True,
    )

    contents = serialize_round(record)

    assert contents.endswith(b"\n")
    assert json.loads(contents) == serialize_round_record(record)
    assert contents == serialize_round(record)


def test_project_state_serializer_rejects_non_portable_round_without_writing() -> None:
    record = RoundRecord(
        round_number=1,
        commit="a" * 40,
        perf_metric=123.0,
        perf_unit="ops/s",
        passed=True,
        evaluation_artifact="/host/result.json",
    )

    with pytest.raises(ProjectStateError, match="portable project-relative path"):
        serialize_round(record)


def test_completed_rounds_use_one_file_per_round_and_are_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    second = RoundRecord(2, "b" * 40, 20.0, "ops/s", passed=True)
    first = RoundRecord(1, "a" * 40, 10.0, "ops/s", passed=True)

    first_snapshot = store.state.save_round(run.run_id, first)
    second_snapshot = store.state.save_round(run.run_id, second)
    assert store.state.save_round(run.run_id, first) == first_snapshot

    assert first_snapshot.files[0].relative_path.name == "0001.json"
    assert second_snapshot.files[0].relative_path.name == "0002.json"
    assert store.state.load_rounds(run.run_id) == [first, second]
    first_path = _rounds_dir(store, run.run_id) / "0001.json"
    assert json.loads(first_path.read_text(encoding="utf-8"))["round"] == 1
    assert first_path.read_bytes() == serialize_round(first)


def test_completed_rounds_must_be_saved_in_append_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    first = RoundRecord(1, "a" * 40, 10.0, "ops/s", passed=True)
    second = RoundRecord(2, "b" * 40, 20.0, "ops/s", passed=True)
    third = RoundRecord(3, "c" * 40, 30.0, "ops/s", passed=True)

    with pytest.raises(ProjectStateError, match="expected round 1, got 2"):
        store.state.save_round(run.run_id, second)

    store.state.save_round(run.run_id, first)
    with pytest.raises(ProjectStateError, match="expected round 2, got 3"):
        store.state.save_round(run.run_id, third)


def test_restoring_a_completed_round_validates_sequence_before_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    second = RoundRecord(2, "b" * 40, 20.0, "ops/s", passed=True)

    with pytest.raises(ProjectStateError, match="without completed round 1"):
        store.state.restore_completed_round(run.run_id, second)

    assert store.state.load_rounds(run.run_id) == []


def test_restoring_a_completed_round_repairs_its_corrupt_local_copy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    first = RoundRecord(1, "a" * 40, 10.0, "ops/s", passed=True)
    store.state.save_round(run.run_id, first)
    target = _rounds_dir(store, run.run_id) / "0001.json"
    target.write_text("{not-json", encoding="utf-8")

    restored = store.state.restore_completed_round(run.run_id, first)

    assert restored == store.state.completed_round_snapshot(run.run_id, 1)
    assert store.state.load_rounds(run.run_id) == [first]


def test_loaded_rounds_must_form_contiguous_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    directory = _rounds_dir(store, run.run_id)
    directory.mkdir(parents=True)
    for round_number in (1, 3):
        record = RoundRecord(
            round_number,
            str(round_number) * 40,
            float(round_number),
            "ops/s",
            passed=True,
        )
        (directory / f"{round_number:04d}.json").write_text(
            json.dumps(serialize_round_record(record)),
            encoding="utf-8",
        )

    with pytest.raises(ProjectStateError, match="expected round 2, found 3"):
        store.state.load_rounds(run.run_id)


def test_completed_round_cannot_be_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    original = RoundRecord(1, "a" * 40, 10.0, "ops/s", passed=True)
    conflicting = RoundRecord(1, "b" * 40, 11.0, "ops/s", passed=True)
    store.state.save_round(run.run_id, original)

    with pytest.raises(ProjectStateError, match="already exists with different data"):
        store.state.save_round(run.run_id, conflicting)


@pytest.mark.parametrize(
    "artifact",
    ["/absolute/result.json", "../result.json", "results\\host.json", ""],
)
def test_completed_round_rejects_machine_local_artifact_paths(
    tmp_path: Path, artifact: str
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    record = RoundRecord(
        1,
        "a" * 40,
        10.0,
        "ops/s",
        passed=True,
        evaluation_artifact=artifact,
    )

    with pytest.raises(ProjectStateError, match="portable project-relative path"):
        store.state.save_round(run.run_id, record)


def test_completed_round_rejects_non_finite_metrics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    record = RoundRecord(1, "a" * 40, float("nan"), "ops/s", passed=True)

    with pytest.raises(ProjectStateError, match="finite numbers"):
        store.state.save_round(run.run_id, record)


def test_loaded_round_rejects_machine_local_artifact_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    path = _rounds_dir(store, run.run_id) / "0001.json"
    path.parent.mkdir(parents=True)
    record = RoundRecord(
        1,
        "a" * 40,
        10.0,
        "ops/s",
        passed=True,
        evaluation_artifact="/absolute/result.json",
    )
    path.write_text(json.dumps(serialize_round_record(record)), encoding="utf-8")

    with pytest.raises(
        ProjectStateError,
        match=r"0001\.json.*portable project-relative path",
    ):
        store.state.load_rounds(run.run_id)


def test_loaded_round_rejects_non_finite_metrics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    path = _rounds_dir(store, run.run_id) / "0001.json"
    path.parent.mkdir(parents=True)
    record = RoundRecord(1, "a" * 40, float("nan"), "ops/s", passed=True)
    path.write_text(json.dumps(serialize_round_record(record)), encoding="utf-8")

    with pytest.raises(ProjectStateError, match=r"0001\.json.*finite numbers"):
        store.state.load_rounds(run.run_id)


def _downgrade_run_schema(store: Project, run_id: str) -> Path:
    """Rewrite one run manifest as a version 1 recording without an environment."""
    path = _run_manifest_path(store, run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    del raw["configuration"]["run_environment"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _downgrade_run_schema_to_v2(store: Project, run_id: str) -> Path:
    """Rewrite one run manifest as a version 2 recording without resources."""
    path = _run_manifest_path(store, run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 2
    del raw["configuration"]["run_environment"]["resources"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_loading_a_pre_environment_run_requires_migration(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _downgrade_run_schema(store, run.run_id)

    with pytest.raises(RunSchemaMigrationRequiredError) as caught:
        store.state.load_run(run.run_id)

    assert caught.value.recorded_version == 1
    assert caught.value.run_id == run.run_id
    assert "run.json" in str(caught.value)
    assert "runtime environment" in str(caught.value)


def test_migrating_a_run_records_the_supplied_environment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _downgrade_run_schema(store, run.run_id)
    environment = RunEnvironmentRecord(name="modal", gpu="H100!", app="vibesys")

    migrated = store.state.migrate_run_environment(run.run_id, environment)

    assert migrated.schema_version == RUN_SCHEMA_VERSION
    assert migrated.configuration.run_environment == environment
    assert store.state.load_run(run.run_id) == migrated
    assert migrated.model_dump(exclude={"schema_version", "configuration"}) == run.model_dump(
        exclude={"schema_version", "configuration"}
    )


def test_migrating_a_version_2_run_preserves_its_environment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _downgrade_run_schema_to_v2(store, run.run_id)

    with pytest.raises(RunSchemaMigrationRequiredError) as caught:
        store.state.load_run(run.run_id)
    assert caught.value.recorded_version == 2

    migrated = store.state.migrate_run_environment(run.run_id, run.configuration.run_environment)

    assert migrated.schema_version == RUN_SCHEMA_VERSION
    assert migrated.configuration.run_environment == run.configuration.run_environment
    assert store.state.load_run(run.run_id) == migrated


def test_migrating_a_version_2_run_rejects_a_different_environment(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _downgrade_run_schema_to_v2(store, run.run_id)

    with pytest.raises(ProjectStateError, match="different run environment"):
        store.state.migrate_run_environment(run.run_id, RunEnvironmentRecord(name="modal"))


def test_run_environment_round_trips_portable_resources(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resources = RunResourceRequest(
        nodes=2,
        accelerators_per_node=4,
        accelerator_backend="rocm",
        cpus_per_node=192,
    )
    configuration = _configuration().model_copy(
        update={"run_environment": RunEnvironmentRecord(name="skypilot", resources=resources)}
    )
    run = store.state.new_run_manifest(
        "Remote Queue",
        branch="vibesys/remote-queue",
        vibesys_version="0.2.0",
        configuration=configuration,
        trusted_input_baseline="a" * 40,
        now=NOW,
        unique=UUID(int=99),
    )
    store.state.create_run(run)

    loaded = store.state.load_run(run.run_id)

    assert loaded.configuration.run_environment.resources == resources


def test_migrating_a_current_run_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)

    with pytest.raises(ProjectStateError, match="already at run schema version"):
        store.state.migrate_run_environment(run.run_id, RunEnvironmentRecord(name="local"))


def test_migrating_an_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    path = _run_manifest_path(store, run.run_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 99
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ProjectStateError, match="unsupported run schema version"):
        store.state.migrate_run_environment(run.run_id, RunEnvironmentRecord(name="local"))


def test_corrupt_metadata_error_names_the_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    path = _run_manifest_path(store, run.run_id)
    path.write_text('{"schema_version": 99}', encoding="utf-8")

    with pytest.raises(ProjectStateError, match=r"Invalid VibeSys metadata.*run\.json"):
        store.state.load_run(run.run_id)


def test_unknown_round_field_is_rejected_with_its_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    path = _rounds_dir(store, run.run_id) / "0001.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "round": 1,
                "commit": None,
                "perf_metric": None,
                "perf_unit": None,
                "passed": False,
                "surprise": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectStateError, match=r"Invalid completed-round.*0001\.json"):
        store.state.load_rounds(run.run_id)


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
    worktrees = _worktrees_dir(store, run.run_id)

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
