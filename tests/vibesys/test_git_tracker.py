"""Contract tests for canonical project Git history."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.git_tracker import GitTracker
from vibesys.run.project_policy import trusted_project_input_paths
from vs_project import PlainRunConfiguration, Project, RunEnvironmentRecord

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project import StateSnapshot

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(  # noqa: S603  # tracked: #288
        ["git", *args],  # noqa: S607  # tracked: #288
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
        env={**os.environ, **_IDENTITY},
    ).stdout.strip()


def _tracker(root: Path, run_id: str = "test-run") -> GitTracker:
    return GitTracker(
        root,
        run_id=run_id,
        events=NullGitTrackerEvents(),
        excluded_dirs=("attempts",),
    )


def _initialized_tracker(root: Path, run_id: str = "test-run") -> GitTracker:
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = _tracker(root, run_id)
    tracker.init(existing=False)
    return tracker


def _project(root: Path, tracker: GitTracker, run_id: str = "test-run") -> Project:
    project = Project.open(root)
    project.state.create_project("Git tracker test")
    assert tracker.project_branch is not None
    assert tracker.trusted_input_baseline is not None
    project.state.create_run(
        project.state.new_run_manifest(
            "Git tracker test",
            run_id=run_id,
            branch=tracker.project_branch,
            vibesys_version="test",
            trusted_input_baseline=tracker.trusted_input_baseline,
            configuration=PlainRunConfiguration(
                outer_loop="plain",
                run_environment=RunEnvironmentRecord(name="local"),
                agent_backend="stub",
                compute_backend="cpu",
                max_rounds=2,
                max_attempts_per_issue=1,
                max_issues_per_perf_eval=1,
            ),
        )
    )
    tracker.snapshot_with_framework_metadata(
        "initialize state", project.state.initialization_snapshot(run_id)
    )
    return project


def _namespace_snapshot(
    project: Project,
    run_id: str,
    namespace: str,
    *files: tuple[str, str | bytes],
) -> StateSnapshot:
    state = project.state.portable_namespace(run_id, namespace)
    root = state.external_directory()
    for relative, contents in files:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, str):
            destination.write_text(contents, encoding="utf-8")
        else:
            destination.write_bytes(contents)
    return state.snapshot()


def _initialize_existing_repository(root: Path) -> str:
    _git(root, "init", "-q", "-b", "main")
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "main.py")
    _git(root, "commit", "-q", "-m", "baseline")
    return _git(root, "rev-parse", "HEAD")


def test_fresh_project_owns_repository_branch_and_local_excludes(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "agent.toml").write_text("secret = true\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")

    tracker = _tracker(tmp_path, "round-trip")
    tracker.init(existing=False)

    assert tracker.root == tmp_path.resolve()
    assert tracker.history_root == tmp_path.resolve()
    assert tracker.project_branch == "vibesys-runs/round-trip"
    assert _git(tmp_path, "branch", "--show-current") == tracker.project_branch
    assert tracker.trusted_input_baseline == tracker.current_sha()
    assert _git(tmp_path, "ls-files").splitlines() == ["main.py"]
    excludes = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert (
        Project.open(tmp_path).state.git_integration("round-trip").local_exclude_pattern in excludes
    )
    assert "/agent.toml" in excludes
    assert "/.codex-tmp/" in excludes
    assert "/.env.*" in excludes
    assert "attempts/" in excludes
    assert not (tmp_path / ".gitignore").exists()


def test_existing_repository_starts_from_clean_baseline(tmp_path: Path) -> None:
    baseline = _initialize_existing_repository(tmp_path)

    tracker = _tracker(tmp_path)
    tracker.init(existing=False)

    assert tracker.trusted_input_baseline == baseline
    assert tracker.current_sha() == baseline
    assert _git(tmp_path, "branch", "--show-current") == "vibesys-runs/test-run"


def test_existing_repository_rejects_dirty_or_unborn_history(tmp_path: Path) -> None:
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _initialize_existing_repository(dirty)
    (dirty / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be clean"):
        _tracker(dirty).init(existing=False)

    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q", "-b", "main")
    with pytest.raises(ValueError, match="no baseline commit"):
        _tracker(unborn).init(existing=False)


def test_project_must_be_repository_root(tmp_path: Path) -> None:
    _initialize_existing_repository(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="repository root"):
        _tracker(nested).init(existing=False)


def test_resume_selects_existing_run_branch_and_baseline(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    baseline = tracker.trusted_input_baseline
    assert baseline is not None
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    tracker.snapshot("candidate")
    expected = tracker.current_sha()
    _git(tmp_path, "switch", "main")

    resumed = _tracker(tmp_path)
    resumed.init(existing=True, trusted_input_baseline=baseline)

    assert _git(tmp_path, "branch", "--show-current") == "vibesys-runs/test-run"
    assert resumed.current_sha() == expected
    assert resumed.trusted_input_baseline == baseline


def test_run_branch_does_not_conflict_with_repository_vibesys_branch(tmp_path: Path) -> None:
    _initialize_existing_repository(tmp_path)
    _git(tmp_path, "branch", "vibesys")

    tracker = _tracker(tmp_path)
    tracker.init(existing=False)

    assert _git(tmp_path, "branch", "--show-current") == "vibesys-runs/test-run"


def test_resume_accepts_legacy_vibesys_run_branch(tmp_path: Path) -> None:
    baseline = _initialize_existing_repository(tmp_path)
    _git(tmp_path, "switch", "-q", "-c", "vibesys/test-run")

    tracker = _tracker(tmp_path)
    tracker.init(existing=True, trusted_input_baseline=baseline)

    assert tracker.project_branch == "vibesys/test-run"


def test_resume_requires_repository_and_existing_run_branch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no Git repository"):
        _tracker(tmp_path).init(existing=True)

    _initialize_existing_repository(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        _tracker(tmp_path).init(existing=True)


def test_private_inputs_must_not_be_recoverable_from_history(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "agent.toml").write_text("secret = true\n", encoding="utf-8")
    _git(tmp_path, "add", "agent.toml")
    _git(tmp_path, "commit", "-q", "-m", "leak")
    (tmp_path / "agent.toml").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "delete leak")

    with pytest.raises(ValueError, match="private inputs"):
        _tracker(tmp_path).init(existing=False)


def test_snapshot_commits_candidate_paths_but_never_local_or_private_state(
    tmp_path: Path,
) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    store.state.local_namespace("test-run", "agent").external_directory().joinpath(
        "state.json"
    ).write_text("{}\n", encoding="utf-8")
    portable = _namespace_snapshot(
        store,
        "test-run",
        "agent",
        ("rogue.json", "{}\n"),
    )
    rogue_pathspec = (
        store.state.git_integration("test-run").resolve_snapshot(portable).files[0].pathspec
    )
    _git(tmp_path, "add", "--force", rogue_pathspec)
    (tmp_path / "agent.toml").write_text("secret = true\n", encoding="utf-8")
    (tmp_path / "module.pyc").write_bytes(b"cache")

    tracker.snapshot("candidate")

    assert _git(tmp_path, "show", "HEAD:main.py") == "VALUE = 2"
    assert rogue_pathspec not in _git(tmp_path, "ls-tree", "-r", "--name-only", "HEAD")


def test_framework_snapshot_commits_only_supplied_exact_metadata(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    snapshot = _namespace_snapshot(
        store,
        "test-run",
        "agent",
        ("rounds/0001.json", b'{"round":1}\n'),
    )

    tracker.snapshot_with_framework_metadata("round 1", snapshot)

    assert _git(tmp_path, "show", "HEAD:main.py") == "VALUE = 2"
    assert tracker.framework_snapshot_status(snapshot).value == "exact"


def test_framework_snapshot_refuses_unexpected_metadata_edits(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    agent_snapshot = _namespace_snapshot(
        store,
        "test-run",
        "agent",
        ("state.json", "old\n"),
    )
    tracker.snapshot_with_framework_metadata("initialize agent", agent_snapshot)
    agent_state = (
        store.state.portable_namespace("test-run", "agent").external_directory() / "state.json"
    )
    agent_state.write_text("tampered\n", encoding="utf-8")
    evolve_snapshot = _namespace_snapshot(
        store,
        "test-run",
        "evolve",
        ("population.json", "new\n"),
    )

    with pytest.raises(ValueError, match="unexpectedly modified"):
        tracker.snapshot_with_framework_metadata("evolve", evolve_snapshot)
    with pytest.raises(ValueError, match="unexpectedly modified"):
        tracker.snapshot_with_framework_metadata("agent", agent_snapshot)


def test_framework_state_snapshot_replaces_one_namespace_exactly(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    initial = _namespace_snapshot(
        store,
        "test-run",
        "evolve",
        ("keep.json", "old\n"),
        ("remove.json", "remove\n"),
    )
    tracker.snapshot_with_framework_metadata("initialize evolve", initial)
    state = store.state.portable_namespace("test-run", "evolve")
    state.external_directory().joinpath("remove.json").unlink()
    state.external_directory().joinpath("keep.json").write_text("new\n", encoding="utf-8")
    nested = state.external_directory("nested") / "add.json"
    nested.write_bytes(b"add\n")
    replacement = state.snapshot()

    tracker.snapshot_framework_state("rotate population", replacement)

    assert tracker.framework_snapshot_status(replacement).value == "exact"
    assert {file.relative_path.as_posix() for file in replacement.files} == {
        "keep.json",
        "nested/add.json",
    }


def test_framework_state_snapshot_preserves_retained_file_inode(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    initial = _namespace_snapshot(
        store,
        "test-run",
        "runtime",
        ("effective-objective.md", "old\n"),
    )
    tracker.snapshot_with_framework_metadata("initialize runtime", initial)
    runtime_file = (
        store.state.portable_namespace("test-run", "runtime").external_directory()
        / "effective-objective.md"
    )
    initial_inode = runtime_file.stat().st_ino
    replacement = _namespace_snapshot(
        store,
        "test-run",
        "runtime",
        ("effective-objective.md", "new\n"),
    )

    tracker.snapshot_framework_state("update runtime", replacement)

    assert runtime_file.read_text(encoding="utf-8") == "new\n"
    assert runtime_file.stat().st_ino == initial_inode


def test_framework_state_snapshot_leaves_candidate_changes_pending(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    initial = _namespace_snapshot(
        store,
        "test-run",
        "evolve",
        ("state.json", "old\n"),
    )
    tracker.snapshot_with_framework_metadata("initialize state", initial)
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "main.py")
    replacement = _namespace_snapshot(
        store,
        "test-run",
        "evolve",
        ("state.json", "new\n"),
    )

    tracker.snapshot_framework_state("update state", replacement)

    assert _git(tmp_path, "show", "HEAD:main.py") == "VALUE = 1"
    assert tracker.framework_snapshot_status(replacement).value == "exact"
    assert _git(tmp_path, "status", "--short", "main.py") == "M  main.py"


def test_framework_state_snapshot_protects_metadata_outside_namespace(
    tmp_path: Path,
) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    agent_snapshot = _namespace_snapshot(
        store,
        "test-run",
        "agent",
        ("state.json", "agent\n"),
    )
    evolve_snapshot = _namespace_snapshot(
        store,
        "test-run",
        "evolve",
        ("state.json", "old\n"),
    )
    tracker.snapshot_with_framework_metadata("initialize agent", agent_snapshot)
    tracker.snapshot_with_framework_metadata("initialize evolve", evolve_snapshot)
    store.state.portable_namespace("test-run", "agent").external_directory().joinpath(
        "state.json"
    ).write_text("tampered\n", encoding="utf-8")
    replacement = _namespace_snapshot(
        store,
        "test-run",
        "evolve",
        ("state.json", "new\n"),
    )

    with pytest.raises(ValueError, match="other committed VibeSys metadata"):
        tracker.snapshot_framework_state("replace", replacement)

    assert tracker.framework_snapshot_status(evolve_snapshot).value == "exact"


def test_checkout_restores_candidate_tree_and_preserves_framework_state(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    store = _project(tmp_path, tracker)
    first = tracker.current_sha()
    assert first is not None
    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    portable = _namespace_snapshot(
        store,
        "test-run",
        "agent",
        ("state.json", "portable\n"),
    )
    tracker.snapshot_with_framework_metadata("candidate", portable)
    portable_file = (
        store.state.portable_namespace("test-run", "agent").external_directory() / "state.json"
    )
    portable_file.write_text("new portable\n", encoding="utf-8")
    local = store.state.local_namespace("test-run", "agent").external_directory() / "active.txt"
    local.write_text("local\n", encoding="utf-8")
    (tmp_path / "scratch.txt").write_text("delete me\n", encoding="utf-8")

    assert tracker.checkout_tree(first, clean=True)

    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert portable_file.read_text(encoding="utf-8") == "new portable\n"
    assert local.read_text(encoding="utf-8") == "local\n"
    assert not (tmp_path / "scratch.txt").exists()


def test_trusted_input_changes_compare_against_branch_point(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    objective = tmp_path / "OBJECTIVE.md"
    objective.write_text("original\n", encoding="utf-8")
    tracker = _tracker(tmp_path)
    tracker.init(existing=False)

    objective.write_text("changed\n", encoding="utf-8")
    assert tracker.trusted_input_changes() == ["OBJECTIVE.md"]
    tracker.snapshot("changed trusted input")
    assert tracker.trusted_input_changes() == ["OBJECTIVE.md"]


def test_repository_tasks_are_trusted_but_generated_state_is_not(tmp_path: Path) -> None:
    task = tmp_path / ".vibesys" / "tasks" / "latency"
    task.mkdir(parents=True)
    objective = task / "OBJECTIVE.md"
    objective.write_text("original\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = GitTracker(
        tmp_path,
        run_id="repository-task",
        events=NullGitTrackerEvents(),
        trusted_input_paths=trusted_project_input_paths(
            tmp_path,
            evaluator_source=None,
        ),
    )
    tracker.init(existing=False)
    _project(tmp_path, tracker, "repository-task")

    assert tracker.trusted_input_changes() == []

    objective.write_text("changed\n", encoding="utf-8")
    assert tracker.trusted_input_changes() == [".vibesys/tasks/latency/OBJECTIVE.md"]
    tracker.snapshot("changed trusted input")
    assert tracker.trusted_input_changes() == [".vibesys/tasks/latency/OBJECTIVE.md"]


def test_candidate_worktree_is_local_and_retained_by_durable_ref(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path, "durable")
    store = _project(tmp_path, tracker, "durable")
    baseline = tracker.current_sha()
    assert baseline is not None
    worktree = store.state.candidate_worktree_directory("durable", "candidate-1")
    tracker.add_worktree(worktree, baseline)
    (worktree / "main.py").write_text("VALUE = 42\n", encoding="utf-8")
    _git(worktree, "add", "main.py")
    _git(worktree, "commit", "-q", "-m", "candidate")
    candidate_sha = _git(worktree, "rev-parse", "HEAD")

    ref = tracker.retain_worktree(worktree, "candidate-1")
    tracker.remove_worktree(worktree)
    _git(tmp_path, "reflog", "expire", "--expire=now", "--all")
    _git(tmp_path, "gc", "--prune=now")

    assert ref == "refs/vibesys/durable/candidates/candidate-1"
    assert _git(tmp_path, "rev-parse", ref) == candidate_sha
    assert _git(tmp_path, "show", f"{ref}:main.py") == "VALUE = 42"
    assert not worktree.exists()


def test_candidate_worktrees_must_use_reserved_directory(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    for path in (tmp_path / "candidate", tmp_path, tmp_path.parent / "outside"):
        with pytest.raises(ValueError, match="below"):
            tracker.add_worktree(path, "HEAD")
        with pytest.raises(ValueError, match="below"):
            tracker.remove_worktree(path)


def test_candidate_ref_rejects_unsafe_id_or_unknown_commit(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)

    with pytest.raises(ValueError, match="candidate id"):
        tracker.retain_candidate("../escape", "HEAD")
    with pytest.raises(ValueError, match="not a commit"):
        tracker.retain_candidate("candidate", "missing")


class _RecordingEvents(NullGitTrackerEvents):
    """Record tracker observations for assertion."""

    def __init__(self) -> None:
        self.snapshots: list[tuple[str, str | None]] = []
        self.baselines: list[str] = []
        self.excluded: list[tuple[str, ...]] = []
        self.warnings: list[str] = []

    def snapshot_recorded(self, label: str, *, commit: str | None) -> None:
        self.snapshots.append((label, commit))

    def baseline_configured(self, commit: str) -> None:
        self.baselines.append(commit)

    def paths_excluded(self, paths: tuple[str, ...]) -> None:
        self.excluded.append(paths)

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        self.warnings.append(summary if detail is None else f"{summary}: {detail}")


def _recording_tracker(root: Path) -> tuple[GitTracker, _RecordingEvents]:
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    events = _RecordingEvents()
    tracker = GitTracker(root, run_id="test-run", events=events, excluded_dirs=())
    tracker.init(existing=False)
    return tracker, events


def test_events_report_baseline_then_each_snapshot_outcome(tmp_path: Path) -> None:
    tracker, events = _recording_tracker(tmp_path)
    assert events.baselines == [tracker.trusted_input_baseline]

    (tmp_path / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    tracker.snapshot("round-1")
    assert events.snapshots[-1] == ("round-1", tracker.current_sha())

    tracker.snapshot("round-2")
    assert events.snapshots[-1] == ("round-2", None)


def test_events_report_explicitly_configured_baseline(tmp_path: Path) -> None:
    tracker, events = _recording_tracker(tmp_path)
    head = tracker.current_sha()
    assert head is not None

    resolved = tracker.configure_trusted_input_baseline(head)

    assert resolved == head
    assert events.baselines[-1] == head


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_events_report_unreadable_path_exclusions(tmp_path: Path) -> None:
    tracker, events = _recording_tracker(tmp_path)
    unreadable = tmp_path / "system_profile.json"
    unreadable.write_text("{}", encoding="utf-8")
    os.chmod(unreadable, 0o000)  # noqa: PTH101
    try:
        tracker.snapshot("round-1")
    finally:
        os.chmod(unreadable, 0o644)  # noqa: PTH101

    assert any("/system_profile.json" in paths for paths in events.excluded)


def test_events_report_failed_tree_restore_as_warning(tmp_path: Path) -> None:
    tracker, events = _recording_tracker(tmp_path)

    assert tracker.checkout_tree("f" * 40) is False
    assert any(warning.startswith("git tree restore") for warning in events.warnings)
