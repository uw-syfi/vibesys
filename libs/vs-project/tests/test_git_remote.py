"""Public contracts for local Git remote operations."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vs_project.api import GitRemoteRepository

if TYPE_CHECKING:
    from pathlib import Path

_IDENTITY = {
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
        env={**os.environ, **_IDENTITY},
    ).stdout.strip()


def _repository(root: Path) -> GitRemoteRepository:
    root.mkdir()
    _git(root, "init", "-q", "-b", "run/test")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "initial")
    return GitRemoteRepository(root)


def test_remote_repository_attaches_one_origin_at_the_repository_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    repository = _repository(root)

    assert repository.origin_url() is None
    assert not repository.has_origin()

    repository.attach_origin("https://example.com/owner/project.git")

    assert repository.has_origin()
    assert repository.origin_url() == "https://example.com/owner/project.git"
    with pytest.raises(ValueError, match="already has an origin"):
        repository.attach_origin("https://example.com/owner/other.git")

    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="repository root"):
        GitRemoteRepository(nested).attach_origin("https://example.com/owner/project.git")


def test_remote_repository_reports_refs_and_pushes_only_explicit_refspecs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    repository = _repository(root)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    repository.attach_origin(str(remote))
    commit = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/product/candidates/one", commit)

    assert repository.current_branch() == "run/test"
    assert repository.upstream() is None
    assert repository.refs("refs/product/candidates/") == ("refs/product/candidates/one",)

    repository.push_origin(("refs/heads/run/test:refs/heads/run/test",))

    assert repository.upstream() == "origin/run/test"
    assert _git(remote, "rev-parse", "refs/heads/run/test") == commit
    assert _git(remote, "for-each-ref", "--format=%(refname)", "refs/product/") == ""
