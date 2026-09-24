"""Failure-mode contracts for project state persistence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict

from vs_loop_state.api import RoundRecord
from vs_project.api import (
    PlainRunConfiguration,
    Project,
    ProjectStateError,
    RunEnvironmentRecord,
    RunManifest,
    StateFile,
    generate_run_id,
    is_project_state_path,
    serialize_round,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: int


class _Other(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str


def _configuration() -> PlainRunConfiguration:
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


def _store(tmp_path: Path) -> Project:
    (tmp_path / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    store = Project.open(tmp_path)
    store.state.create_project("Queue SPSC", now=NOW)
    return store


def _project_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


def _manifest(store: Project, *, branch: str = "vibesys/queue") -> RunManifest:
    return store.state.new_run_manifest(
        "Queue SPSC",
        branch=branch,
        vibesys_version="0.2.0",
        configuration=_configuration(),
        trusted_input_baseline="a" * 40,
        now=NOW,
        unique=UUID(int=1),
    )


def _run(store: Project) -> RunManifest:
    manifest = _manifest(store)
    store.state.create_run(manifest)
    return manifest


def _run_json(store: Project, run_id: str) -> Path:
    return store.state.project_root / ".vibesys/state/runs" / run_id / "run.json"


def _rounds_dir(store: Project, run_id: str) -> Path:
    return store.state.project_root / ".vibesys/state/runs" / run_id / "agent/rounds"


def _record(number: int) -> RoundRecord:
    return RoundRecord(number, f"{number:x}" * 40, 10.0, "ops/s", passed=True)


def _fail_for(monkeypatch: pytest.MonkeyPatch, method: str, name: str, error: OSError) -> None:
    """Make ``Path.<method>`` raise ``error`` for paths whose final component is ``name``."""
    real = getattr(Path, method)

    def flaky(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == name:
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, flaky)


@pytest.mark.parametrize("unsafe", ["../x", "/abs/path", ""])
def test_is_project_state_path_rejects_unsafe_paths(unsafe: str) -> None:
    with pytest.raises(ProjectStateError, match="safe relative path"):
        is_project_state_path(unsafe)


def test_state_file_requires_bytes_contents_and_posix_path() -> None:
    with pytest.raises(TypeError, match="contents must be bytes"):
        StateFile(relative_path=PurePosixPath("a.json"), contents=cast("Any", "text"))
    with pytest.raises(TypeError, match="paths must be PurePosixPath values"):
        StateFile(relative_path=cast("Any", "a.json"), contents=b"{}")


def test_generate_run_id_and_new_manifest_require_timezone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    naive = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)

    with pytest.raises(ProjectStateError, match="Metadata timestamp must include a timezone"):
        store.state.new_run_manifest(
            "x",
            branch="vibesys/x",
            vibesys_version="0.2.0",
            configuration=_configuration(),
            trusted_input_baseline="a" * 40,
            now=naive,
        )
    with pytest.raises(ProjectStateError, match="Run ID timestamp must include a timezone"):
        generate_run_id("x", now=naive)


def test_create_run_rejects_different_data_for_existing_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)
    changed = manifest.model_copy(update={"branch": "vibesys/other"})

    with pytest.raises(ProjectStateError, match="already exists with different data"):
        store.state.create_run(changed)

    assert store.state.load_run(manifest.run_id) == manifest


def test_list_runs_rejects_stray_file_in_runs_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _run(store)
    stray = store.state.project_root / ".vibesys/state/runs/stray.txt"
    stray.write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Unexpected file in VibeSys runs directory"):
        store.state.list_runs()


def test_current_run_pointer_read_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _run(store)
    assert store.state.current_run_id() is not None
    _fail_for(monkeypatch, "read_text", "current-run", PermissionError("denied"))

    with pytest.raises(ProjectStateError, match=r"Could not read current run pointer .*denied"):
        store.state.current_run_id()


def _write_legacy_run(store: Project, run_id: str, version: object, configuration: object) -> None:
    manifest = _manifest(store)
    raw = json.loads(manifest.model_dump_json())
    raw["run_id"] = run_id
    raw["schema_version"] = version
    raw["configuration"] = configuration
    path = _run_json(store, run_id)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(raw), encoding="utf-8")


def _configuration_dict(*, with_environment: bool) -> dict[str, object]:
    raw = json.loads(_configuration().model_dump_json())
    if not with_environment:
        del raw["run_environment"]
    return raw


@pytest.mark.parametrize(
    ("version", "configuration", "message"),
    [
        (1, None, "has no configuration object"),
        (
            1,
            _configuration_dict(with_environment=True),
            "already records a run environment",
        ),
        (
            2,
            {**_configuration_dict(with_environment=True), "run_environment": "nope"},
            "has an invalid run environment",
        ),
        (
            2,
            {
                **_configuration_dict(with_environment=True),
                "run_environment": {"name": "docker"},
            },
            "records a different run environment",
        ),
        (
            1,
            {"outer_loop": "plain"},
            "Could not migrate VibeSys metadata",
        ),
        (7, _configuration_dict(with_environment=True), "unsupported run schema version 7"),
    ],
)
def test_migrate_run_environment_rejects_inconsistent_manifests(
    tmp_path: Path, version: int, configuration: object, message: str
) -> None:
    store = _store(tmp_path)
    _write_legacy_run(store, "legacy-run", version, configuration)
    before = _run_json(store, "legacy-run").read_text(encoding="utf-8")

    with pytest.raises(ProjectStateError, match=message):
        store.state.migrate_run_environment("legacy-run", RunEnvironmentRecord(name="local"))

    assert _run_json(store, "legacy-run").read_text(encoding="utf-8") == before


def test_migrate_run_environment_rejects_current_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _run(store)

    with pytest.raises(ProjectStateError, match="already at run schema version"):
        store.state.migrate_run_environment(manifest.run_id, RunEnvironmentRecord(name="local"))


def test_load_rounds_rejects_unexpected_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    store.state.save_round(run.run_id, _record(1))
    (_rounds_dir(store, run.run_id) / "notes.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Unexpected completed-round entry"):
        store.state.load_rounds(run.run_id)


def test_load_rounds_rejects_file_naming_another_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    store.state.save_round(run.run_id, _record(1))
    (_rounds_dir(store, run.run_id) / "0001.json").write_bytes(serialize_round(_record(2)))

    with pytest.raises(ProjectStateError, match="contains round 2, expected 1"):
        store.state.load_rounds(run.run_id)


def test_restore_rejects_unexpected_or_duplicate_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    rounds = _rounds_dir(store, run.run_id)
    rounds.mkdir(parents=True)
    (rounds / "readme.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Unexpected completed-round entry"):
        store.state.restore_completed_round(run.run_id, _record(1))

    (rounds / "readme.txt").unlink()
    (rounds / "1.json").write_text("{}", encoding="utf-8")
    (rounds / "0001.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Duplicate completed-round number 1"):
        store.state.restore_completed_round(run.run_id, _record(1))


def test_restore_rejects_a_round_before_an_existing_later_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    store.state.save_round(run.run_id, _record(1))
    store.state.save_round(run.run_id, _record(2))

    with pytest.raises(ProjectStateError, match="Cannot restore round 1 before existing round 2"):
        store.state.restore_completed_round(run.run_id, _record(1))


def test_restore_rejects_predecessor_naming_another_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    rounds = _rounds_dir(store, run.run_id)
    rounds.mkdir(parents=True)
    (rounds / "0001.json").write_text(serialize_round(_record(3)).decode("utf-8"), encoding="utf-8")

    with pytest.raises(ProjectStateError, match="contains round 3, expected 1"):
        store.state.restore_completed_round(run.run_id, _record(2))


def test_namespace_apply_deletion_rejects_directory_at_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    slot = namespace.slot("cursor.json", _Cursor)
    (namespace.external_directory() / "cursor.json").mkdir()

    with pytest.raises(ProjectStateError, match="state path is not a file"):
        slot.apply(slot.transition(None))
    with pytest.raises(ProjectStateError, match="state path is not a file"):
        namespace.delete("cursor.json")


def test_namespace_delete_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    namespace.save("cursor.json", _Cursor(round=1))
    _fail_for(monkeypatch, "unlink", "cursor.json", PermissionError("denied"))

    with pytest.raises(ProjectStateError, match=r"Could not delete VibeSys state model .*denied"):
        namespace.delete("cursor.json")

    assert namespace.load("cursor.json", _Cursor) == _Cursor(round=1)


def test_namespace_snapshot_rejects_unsupported_file_types(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    os.mkfifo(namespace.external_directory() / "pipe")

    with pytest.raises(ProjectStateError, match="contains an unsupported file type"):
        namespace.snapshot()


def test_namespace_snapshot_read_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "plain")
    namespace.save("cursor.json", _Cursor(round=1))
    _fail_for(monkeypatch, "read_bytes", "cursor.json", PermissionError("denied"))

    with pytest.raises(
        ProjectStateError, match=r"Could not snapshot VibeSys portable state .*denied"
    ):
        namespace.snapshot()


def test_local_namespace_has_no_worktree_equivalent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.local_namespace(run.run_id, "agent")

    with pytest.raises(ProjectStateError, match="has no worktree equivalent"):
        namespace.equivalent_external_file(tmp_path, "active.json")


def test_equivalent_external_file_requires_a_directory_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Project root is not a directory"):
        namespace.equivalent_external_file(not_a_directory, "state.json")

    assert namespace.equivalent_external_file(tmp_path, "state.json") == (
        tmp_path / ".vibesys/state/runs" / run.run_id / "agent/state.json"
    )


def test_external_directory_rejects_a_file_at_the_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    (namespace.external_directory() / "occupied").write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="external state path is not a directory"):
        namespace.external_directory("occupied")


def test_external_directory_creation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    _fail_for(monkeypatch, "mkdir", "fresh", PermissionError("denied"))

    with pytest.raises(
        ProjectStateError,
        match=r"Could not create VibeSys portable external state directory .*denied",
    ):
        namespace.external_directory("fresh")


def test_namespace_rejects_a_file_in_place_of_its_directory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    directory = namespace.external_directory()
    directory.rmdir()
    directory.write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="portable state namespace is not a directory"):
        namespace.snapshot()


def test_namespace_validation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    namespace.external_directory()
    _fail_for(monkeypatch, "is_dir", "agent", PermissionError("denied"))

    with pytest.raises(
        ProjectStateError, match=r"Could not validate VibeSys portable state namespace .*denied"
    ):
        namespace.snapshot()


def test_slot_rejects_non_bytes_and_foreign_schema_transitions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    namespace = store.state.portable_namespace(run.run_id, "agent")
    cursor_slot = namespace.slot("state.json", _Cursor)
    other_slot = namespace.slot("state.json", _Other)
    transition = cursor_slot.transition(_Cursor(round=1))

    with pytest.raises(TypeError, match="serialized state transition must be bytes"):
        cursor_slot.deserialize_transition(cast("Any", "{}"))
    with pytest.raises(ProjectStateError, match="does not match the typed slot schema"):
        other_slot.validate_transition(transition)
    with pytest.raises(ProjectStateError, match="does not match the typed slot schema"):
        other_slot.apply(transition)

    assert namespace.load_optional("state.json", _Cursor) is None


def test_find_projects_reports_unreadable_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(_project_dir(tmp_path))
    collection = tmp_path / "collection"
    collection.mkdir()
    _fail_for(monkeypatch, "iterdir", "collection", PermissionError("denied"))

    with pytest.raises(ProjectStateError, match=r"Could not inspect project collection .*denied"):
        type(store.state).find_projects(collection)


def test_log_directory_for_rejects_file_project_root(tmp_path: Path) -> None:
    store = _store(_project_dir(tmp_path))
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Project root is not a directory"):
        type(store.state).log_directory_for(not_a_directory, "run-1")


def _force_false(monkeypatch: pytest.MonkeyPatch, method: str, name: str) -> None:
    """Make ``Path.<method>`` report False for paths whose final component is ``name``."""
    real = getattr(Path, method)

    def patched(self: Path, *args: object, **kwargs: object) -> object:
        return False if self.name == name else real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, patched)


def test_project_state_requires_an_existing_project_root(tmp_path: Path) -> None:
    store = _store(_project_dir(tmp_path))
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="Project root is not a directory"):
        type(store.state)(not_a_directory)


def test_state_home_must_not_be_inside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_dir(tmp_path)
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(project / "state-home"))

    with pytest.raises(ProjectStateError, match="must place machine-local state outside"):
        _ = Project.open(project).state


def test_state_home_creation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_dir(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(blocker / "home"))

    with pytest.raises(ProjectStateError, match=r"Could not create VibeSys state home .*blocker"):
        _ = Project.open(project).state


def test_legacy_local_state_migration_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_dir(tmp_path)
    legacy = project / ".vibesys/state/local"
    legacy.mkdir(parents=True)
    (legacy / "current-run").write_text("old-run\n", encoding="utf-8")

    def broken_copytree(*_args: object, **_kwargs: object) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr("shutil.copytree", broken_copytree)

    with pytest.raises(
        ProjectStateError, match=r"Could not migrate VibeSys local state .*disk full"
    ):
        _ = Project.open(project).state

    assert (legacy / "current-run").read_text(encoding="utf-8") == "old-run\n"


def test_storage_root_must_be_a_directory(tmp_path: Path) -> None:
    project = _project_dir(tmp_path)
    (project / ".vibesys").mkdir()
    (project / ".vibesys/state").write_text("x", encoding="utf-8")

    with pytest.raises(ProjectStateError, match="VibeSys metadata root is not a directory"):
        _ = Project.open(project).state


def test_local_state_root_must_not_resolve_outside_the_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_dir(tmp_path)
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "projects").symlink_to(outside)
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(home))

    with pytest.raises(ProjectStateError, match=r"VibeSys local metadata root escapes .*outside"):
        _ = Project.open(project).state


def test_storage_root_validation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_dir(tmp_path)
    (project / ".vibesys/state").mkdir(parents=True)
    _fail_for(monkeypatch, "is_symlink", "state", PermissionError("denied"))

    with pytest.raises(
        ProjectStateError, match=r"Could not validate VibeSys metadata root .*denied"
    ):
        _ = Project.open(project).state


def test_local_gitignore_read_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_dir(tmp_path)
    store = Project.open(project)
    store.state.create_project("Queue", now=NOW)
    _fail_for(monkeypatch, "read_text", ".gitignore", PermissionError("denied"))

    with pytest.raises(ProjectStateError, match=r"Could not read VibeSys ignore contract .*denied"):
        store.state.create_project("Queue", now=NOW)


def test_run_manifest_snapshot_rejects_manifest_that_is_not_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _force_false(monkeypatch, "is_file", "run.json")

    with pytest.raises(ProjectStateError, match=r"snapshot path is not a file: .*run\.json"):
        store.state.run_manifest_snapshot(run.run_id)


def test_run_manifest_snapshot_rejects_missing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _force_false(monkeypatch, "is_file", "run.json")
    _force_false(monkeypatch, "exists", "run.json")

    with pytest.raises(ProjectStateError, match=r"snapshot file does not exist: .*run\.json"):
        store.state.run_manifest_snapshot(run.run_id)


def test_run_manifest_snapshot_read_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _fail_for(monkeypatch, "read_bytes", "run.json", PermissionError("denied"))

    with pytest.raises(
        ProjectStateError, match=r"Could not read portable VibeSys snapshot .*denied"
    ):
        store.state.run_manifest_snapshot(run.run_id)


def test_portable_run_export_inspection_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _fail_for(monkeypatch, "rglob", run.run_id, PermissionError("denied"))

    with pytest.raises(
        ProjectStateError, match=r"Could not inspect portable VibeSys snapshot .*denied"
    ):
        store.state.portable_run_export(run.run_id)


@pytest.mark.parametrize("method", ["exists", "is_symlink"])
def test_state_directory_validation_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    store = _store(tmp_path)
    run = _run(store)
    _fail_for(monkeypatch, method, "probe-namespace", PermissionError("denied"))

    with pytest.raises(ProjectStateError, match=r"Could not validate VibeSys .*denied"):
        store.state.portable_namespace(run.run_id, "probe-namespace")


def test_replacement_snapshot_must_select_a_dedicated_run_namespace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _run(store)
    run_wide_snapshot = store.state.portable_run_export(run.run_id)

    with pytest.raises(ValueError, match="must select a dedicated namespace for run"):
        store.state.git_integration(run.run_id).resolve_replacement_snapshot(run_wide_snapshot)


def test_input_fingerprint_rejects_unsupported_file_types(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.mkfifo(tmp_path / "pipe")

    with pytest.raises(ProjectStateError, match=r"Unsupported input file type: .*pipe"):
        store.state.input_fingerprint()


def test_input_fingerprint_read_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    (tmp_path / "data.bin").write_bytes(b"x")
    _fail_for(monkeypatch, "open", "data.bin", PermissionError("denied"))

    with pytest.raises(ProjectStateError, match=r"Could not fingerprint project input .*denied"):
        store.state.input_fingerprint()
