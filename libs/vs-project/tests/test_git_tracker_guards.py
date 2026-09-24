"""Guard and error-path contracts for :class:`vs_project.api.GitTracker`."""

from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vs_project.api import (
    GitTracker,
    NullGitTrackerEvents,
    PlainRunConfiguration,
    Project,
    RunEnvironmentRecord,
)

if TYPE_CHECKING:
    from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str) -> str:
    result = run_test_command(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    )
    return result.stdout.strip()


def _tracker(root: Path, run_id: str = "guard-run") -> GitTracker:
    return GitTracker(root, run_id=run_id, events=NullGitTrackerEvents())


def _committed_repository(root: Path) -> str:
    _git(root, "init", "-q", "-b", "main")
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "main.py")
    _git(root, "commit", "-q", "-m", "baseline")
    return _git(root, "rev-parse", "HEAD")


def _initialized_tracker(root: Path, run_id: str = "guard-run") -> GitTracker:
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = _tracker(root, run_id)
    tracker.init(existing=False)
    return tracker


def _project(root: Path, tracker: GitTracker) -> Project:
    project = Project.open(root)
    project.state.create_project("Guard test")
    assert tracker.trusted_input_baseline is not None
    project.state.create_run(
        project.state.new_run_manifest(
            "Guard test",
            run_id=tracker.run_id,
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
        "initialize state", project.state.initialization_snapshot(tracker.run_id)
    )
    return project


@pytest.mark.parametrize("bad", ["/abs/path", "", ".", "a/../b", "../up"])
def test_trusted_input_paths_must_be_normalized_relative(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match=r"project path must be a normalized relative path"):
        GitTracker(
            tmp_path,
            run_id="guard-run",
            events=NullGitTrackerEvents(),
            trusted_input_paths=[bad],
        )


def test_project_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match=r"project root must be an existing directory"):
        _tracker(missing)
    file_root = tmp_path / "file"
    file_root.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match=r"project root must be an existing directory"):
        _tracker(file_root)


def test_retain_worktree_reports_git_failure_in_candidate_worktree(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    worktree = Project.open(tmp_path).state.candidate_worktree_directory("guard-run", "cand-1")
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    with pytest.raises(RuntimeError, match=r"Git command failed in candidate worktree"):
        tracker.retain_worktree(worktree, "cand-1")


def test_candidate_patch_requires_a_resolvable_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _initialized_tracker(tmp_path)
    sha = tracker.current_sha()
    assert sha is not None

    def no_roots(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(tracker, "run", no_roots)
    with pytest.raises(
        ValueError, match=re.escape(f"cannot resolve workspace baseline for commit {sha}")
    ):
        tracker.candidate_patch(sha)


def test_framework_namespace_must_be_a_directory(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    project = _project(tmp_path, tracker)
    namespace = project.state.portable_namespace("guard-run", "evolve")
    (namespace.external_directory() / "state.json").write_text("{}\n", encoding="utf-8")
    snapshot = namespace.snapshot()
    root = namespace.external_directory()
    (root / "state.json").unlink()
    root.rmdir()
    root.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"framework state namespace is not a directory"):
        tracker.snapshot_framework_state("replace", snapshot)
    assert root.read_text(encoding="utf-8") == "not a directory\n"


def test_current_sha_is_none_when_git_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = _initialized_tracker(tmp_path)
    assert tracker.current_sha() is not None

    def unavailable(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        message = "git is not installed"
        raise FileNotFoundError(message)

    monkeypatch.setattr(tracker, "run", unavailable)
    assert tracker.current_sha() is None


class _RecordingEvents(NullGitTrackerEvents):
    def __init__(self) -> None:
        self.warnings: list[tuple[str, str | None]] = []

    def warning(self, message: str, *, detail: str | None = None) -> None:
        self.warnings.append((message, detail))


def test_checkout_tree_reports_failed_restore_of_preserved_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    events = _RecordingEvents()
    tracker = GitTracker(tmp_path, run_id="guard-run", events=events)
    tracker.init(existing=False)
    sha = tracker.current_sha()
    assert sha is not None
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "notes.txt").write_text("keep\n", encoding="utf-8")

    def restore_fails(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # The rollback replaces the preserved directory with a file, so both
        # the restore command and the later re-application of memory fail.
        (memory / "notes.txt").unlink()
        memory.rmdir()
        memory.write_text("blocker\n", encoding="utf-8")
        raise subprocess.CalledProcessError(1, ["git", "restore"])

    monkeypatch.setattr(tracker, "run", restore_fails)

    assert tracker.checkout_tree(sha, preserve_paths=["memory"]) is False
    messages = [message for message, _detail in events.warnings]
    assert messages == [
        "failed to restore preserved workspace memory after tree restore error",
        f"git tree restore {sha[:8]} failed",
    ]


@pytest.mark.parametrize("bad", ["/abs/memory", "", "a/../../etc"])
def test_checkout_tree_rejects_unsafe_preserve_paths(tmp_path: Path, bad: str) -> None:
    tracker = _initialized_tracker(tmp_path)
    sha = tracker.current_sha()
    assert sha is not None
    with pytest.raises(ValueError, match=r"preserved path must be workspace-relative"):
        tracker.checkout_tree(sha, preserve_paths=[bad])


def test_trusted_input_baseline_must_be_an_existing_commit(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    with pytest.raises(ValueError, match=r"trusted input baseline 'deadbeef' is not a commit"):
        tracker.configure_trusted_input_baseline("deadbeef")
    assert tracker.trusted_input_baseline != "deadbeef"


def test_trusted_input_baseline_must_be_an_ancestor_of_head(tmp_path: Path) -> None:
    tracker = _initialized_tracker(tmp_path)
    _git(tmp_path, "switch", "-q", "-c", "side", "main")
    (tmp_path / "side.py").write_text("SIDE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "side.py")
    _git(tmp_path, "commit", "-q", "-m", "side")
    side_sha = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "switch", "-q", tracker.project_branch)
    before = tracker.trusted_input_baseline

    with pytest.raises(ValueError, match=r"is not an ancestor of HEAD"):
        tracker.configure_trusted_input_baseline(side_sha)
    assert tracker.trusted_input_baseline == before


@pytest.mark.parametrize("run_id", ["bad..id", "trailing.lock", "ends-with-dot."])
def test_run_id_must_form_a_valid_git_branch(tmp_path: Path, run_id: str) -> None:
    tracker = _tracker(tmp_path, run_id)
    with pytest.raises(ValueError, match=r"invalid VibeSys run id for a Git branch"):
        tracker.init(existing=False)
    assert not (tmp_path / ".git").exists()


def test_trusted_input_baseline_is_only_valid_when_resuming(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = _tracker(tmp_path)
    with pytest.raises(ValueError, match=r"only valid when resuming a run"):
        tracker.init(existing=False, trusted_input_baseline="deadbeef")


def test_start_fails_when_baseline_commit_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = _tracker(tmp_path)
    monkeypatch.setattr(tracker, "current_sha", lambda: None)
    with pytest.raises(ValueError, match=r"user-project baseline commit could not be resolved"):
        tracker.init(existing=False)
    assert tracker.trusted_input_baseline is None


def test_start_refuses_existing_run_branch(tmp_path: Path) -> None:
    _committed_repository(tmp_path)
    tracker = _tracker(tmp_path)
    _git(tmp_path, "branch", tracker.project_branch)
    with pytest.raises(
        ValueError, match=re.escape(f"VibeSys run branch already exists: {tracker.project_branch}")
    ):
        tracker.init(existing=False)
    assert _git(tmp_path, "branch", "--show-current") == "main"


def test_init_fails_when_project_history_cannot_be_inspected(tmp_path: Path) -> None:
    _committed_repository(tmp_path)
    blob = _git(tmp_path, "rev-parse", "HEAD:main.py")
    (tmp_path / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    tracker = _tracker(tmp_path)
    with pytest.raises(ValueError, match=r"cannot inspect project Git history for private inputs"):
        tracker.init(existing=False)
