"""Root-identity contracts for application run state."""

from pathlib import Path

import pytest

from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.git_tracker import GitTracker
from vibesys.run.state import RunState
from vs_project import Project


def _tracker(root: Path, run_id: str) -> GitTracker:
    return GitTracker(root, run_id=run_id, events=NullGitTrackerEvents())


def test_run_state_rejects_different_project_and_git_roots(tmp_path: Path) -> None:
    state_root = tmp_path / "state-project"
    git_root = tmp_path / "git-project"
    state_root.mkdir()
    git_root.mkdir()

    with pytest.raises(ValueError, match="project root does not match Git history root"):
        RunState(Project.open(state_root), _tracker(git_root, "run-1"), "run-1")


def test_run_state_rejects_different_state_and_git_run_ids(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    with pytest.raises(ValueError, match="state ID does not match Git history run ID"):
        RunState(Project.open(project_root), _tracker(project_root, "run-1"), "run-2")
