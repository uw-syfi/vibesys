"""GitRepository contract: host Git runs no program an agent-written repository names.

An agent can write its workspace's repository: hooks, config, and in a linked
worktree the ``.git`` file that says where the repository is. Host Git commands on
that workspace run outside any confinement, so a hook, a file-system monitor, or a
``.git`` file pointing at an agent-made repository would run agent code on the host
at the next snapshot. Each case plants such a program as a script that leaves a
marker file, drives the operations a snapshot runs, and requires no marker.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitRepository
    from vs_project.api.testing import ContractProject

REQUIRES_ON_DISK_REPOSITORY = True
"""The planted programs live in a real ``.git``; see ``conftest.ON_DISK``."""

_HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout")


def _plant_program(path: Path, marker: Path) -> Path:
    """An executable script at ``path`` that creates ``marker`` when anything runs it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _plant_repository_programs(git_dir: Path, marker: Path) -> None:
    """Every hook in ``_HOOKS`` and a ``core.fsmonitor`` program, in ``git_dir``."""
    for hook in _HOOKS:
        _plant_program(git_dir / "hooks" / hook, marker)
    monitor = _plant_program(git_dir / "fsmonitor", marker)
    with (git_dir / "config").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\tfsmonitor = {monitor}\n")


def _snapshot(repo: GitRepository, root: Path) -> None:
    """The Git operations a snapshot runs, after an edit."""
    (root / "a.txt").write_text("edited\n", encoding="utf-8")
    repo.unversioned_paths(include_ignored=False)
    repo.uncommitted_paths(["."])
    repo.stage_all(["."])
    repo.commit("snapshot")
    repo.restore_worktree("HEAD")


@pytest.mark.parametrize("pinned", [False, True], ids=["discovered", "bound"])
def test_hooks_and_the_fsmonitor_a_repository_names_never_run(
    sandbox: ContractProject, tmp_path: Path, *, pinned: bool
) -> None:
    sandbox.start({"a.txt": "1\n"})
    marker = tmp_path / "ran"
    _plant_repository_programs(sandbox.root / ".git", marker)
    repo = sandbox.reopen()
    if pinned:
        repo.bind()

    _snapshot(repo, sandbox.root)

    assert not marker.exists()


def test_a_pinned_linked_worktree_ignores_a_redirected_git_file(
    sandbox: ContractProject, tmp_path: Path
) -> None:
    base = sandbox.start({"a.txt": "1\n"})
    destination = tmp_path / "linked"
    sandbox.repo.add_worktree(destination, base)
    location = sandbox.repo.linked_worktree_location(destination)
    linked = sandbox.factory(destination)
    linked.pin(location)
    # The agent points the worktree at a repository it made, with programs planted.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    decoy_repo = sandbox.factory(decoy)
    decoy_repo.initialize(initial_branch="main")
    marker = tmp_path / "ran"
    _plant_repository_programs(decoy / ".git", marker)
    (destination / ".git").write_text(f"gitdir: {decoy / '.git'}\n", encoding="utf-8")

    _snapshot(linked, destination)

    assert not marker.exists()
    head = sandbox.repo.worktree_head(destination)
    assert head != base
    assert sandbox.repo.is_ancestor(base, head)
    assert decoy_repo.head() is None
