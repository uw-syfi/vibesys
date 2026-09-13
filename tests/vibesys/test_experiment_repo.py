"""Tests for remote publication of canonical project history."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003  # tracked: #288

import pytest

from vibesys.repository import RepositoryVisibility
from vibesys.run.experiment_repo import ExperimentRepository
from vibesys.run.git_events import NullGitTrackerEvents
from vibesys.run.git_tracker import GitTracker
from vs_github import GitHubCLI

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # tracked: #288
        ["git", *args],  # noqa: S607  # tracked: #288
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_IDENTITY},
    ).stdout.strip()


def _project(root: Path, run_id: str = "publish-test") -> GitTracker:
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    tracker = GitTracker(root, run_id=run_id, events=NullGitTrackerEvents())
    tracker.init(existing=False)
    return tracker


def test_push_publishes_exact_current_run_branch_without_authoring_history(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tracker = _project(project)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    messages: list[str] = []
    publisher = ExperimentRepository(project, messages.append)
    publisher.attach_remote(str(remote))
    before = tracker.current_sha()

    publisher.push()

    branch = "vibesys-runs/publish-test"
    assert tracker.current_sha() == before
    assert _git(remote, "rev-parse", f"refs/heads/{branch}") == before
    assert publisher.current_run_branch_tracks_origin()
    assert _git(project, "status", "--porcelain") == ""
    assert messages == [
        "[repo] attached origin remote",
        f"[repo] pushed {branch} to origin",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/project.git",
        "git@github.com:example/project.git",
    ],
)
def test_origin_matches_requested_github_repository(tmp_path: Path, url: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    publisher = ExperimentRepository(project, lambda _message: None)
    _git(project, "init", "-q")
    publisher.attach_remote(url)

    assert publisher.origin_matches("example/project")
    assert not publisher.origin_matches("other/project")


def test_origin_rejects_non_github_lookalike(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    publisher = ExperimentRepository(project, lambda _message: None)
    _git(project, "init", "-q")
    publisher.attach_remote("https://evil.example/example/project.git")

    assert not publisher.origin_matches("example/project")


def test_run_branch_tracking_origin_main_is_not_published(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tracker = _project(project)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    publisher = ExperimentRepository(project, lambda _message: None)
    publisher.attach_remote(str(remote))
    current_sha = tracker.current_sha()
    assert current_sha is not None
    _git(project, "branch", "-f", "main", current_sha)
    _git(project, "push", "-q", "-u", "origin", "main")
    _git(
        project,
        "branch",
        "--set-upstream-to",
        "origin/main",
        "vibesys-runs/publish-test",
    )

    assert not publisher.current_run_branch_tracks_origin()


def test_push_publishes_retained_candidates_for_current_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tracker = _project(project)
    candidate_ref = tracker.retain_candidate("candidate-1", "HEAD")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    publisher = ExperimentRepository(project, lambda _message: None)
    publisher.attach_remote(str(remote))

    publisher.push()

    assert _git(remote, "rev-parse", candidate_ref) == tracker.current_sha()


def test_push_accepts_legacy_run_branch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tracker = _project(project)
    legacy_branch = "vibesys/publish-test"
    _git(project, "branch", "-m", legacy_branch)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    publisher = ExperimentRepository(project, lambda _message: None)
    publisher.attach_remote(str(remote))

    publisher.push()

    assert _git(remote, "rev-parse", f"refs/heads/{legacy_branch}") == tracker.current_sha()


def test_push_without_origin_is_a_noop(tmp_path: Path) -> None:
    _project(tmp_path)
    messages: list[str] = []

    ExperimentRepository(tmp_path, messages.append).push()

    assert messages == []


def test_push_never_stages_or_commits_pending_worktree_changes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    tracker = _project(project)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    publisher = ExperimentRepository(project, lambda _message: None)
    publisher.attach_remote(str(remote))
    before = tracker.current_sha()
    (project / "uncommitted.py").write_text("VALUE = 2\n", encoding="utf-8")

    publisher.push()

    assert tracker.current_sha() == before
    assert _git(project, "status", "--short") == "?? uncommitted.py"
    assert _git(remote, "rev-parse", "refs/heads/vibesys-runs/publish-test") == before


def test_push_rejects_non_run_branch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    _git(project, "switch", "main")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    publisher = ExperimentRepository(project, lambda _message: None)
    publisher.attach_remote(str(remote))

    with pytest.raises(ValueError, match="current VibeSys run branch"):
        publisher.push()


@dataclass(frozen=True)
class _RecordingGitHub(GitHubCLI):
    calls: list[tuple[str, str, Path]] = field(default_factory=list)

    def create_repository(
        self,
        repository: str,
        *,
        visibility: str,
        source: Path,
        remote_name: str = "origin",  # noqa: ARG002  # tracked: #288
    ) -> None:
        self.calls.append((repository, visibility, source))


def test_create_remote_delegates_creation_and_attachment(tmp_path: Path) -> None:
    _project(tmp_path)
    github = _RecordingGitHub()
    messages: list[str] = []
    publisher = ExperimentRepository(tmp_path, messages.append, github=github)

    publisher.create_remote(
        "vibesys-playground/example",
        RepositoryVisibility.INTERNAL,
    )

    assert github.calls == [
        ("vibesys-playground/example", "internal", tmp_path),
    ]
    assert messages == ["[repo] created GitHub repository vibesys-playground/example"]


def test_create_or_attach_rejects_existing_origin(tmp_path: Path) -> None:
    _project(tmp_path)
    publisher = ExperimentRepository(tmp_path, lambda _message: None)
    publisher.attach_remote("https://example.com/origin.git")

    with pytest.raises(ValueError, match="already has an origin"):
        publisher.attach_remote("https://example.com/other.git")
    with pytest.raises(ValueError, match="already has an origin"):
        publisher.create_remote("owner/name", RepositoryVisibility.PRIVATE)


def test_publication_requires_repository_root(tmp_path: Path) -> None:
    _project(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()

    with pytest.raises(ValueError, match="repository root"):
        ExperimentRepository(nested, lambda _message: None).attach_remote("remote")
