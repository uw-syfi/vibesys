"""GitRepository contract: host Git runs no program an agent-written repository names.

An agent can write its workspace's repository: hooks, config, in a linked worktree the
``.git`` file that says where the repository is, and any repository nested below it. Host Git commands on
that workspace run outside any confinement, so a hook, a file-system monitor, or a
``.git`` file pointing at an agent-made repository would run agent code on the host
at the next snapshot. Each case plants such a program as a script that leaves a
marker file, drives the operations a snapshot runs, and requires no marker.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest
from tests.support.real_git import REAL_GIT

from vs_project.api import NestedRepositoryError

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


_AGENT_IDENTITY = ("-c", "user.name=agent", "-c", "user.email=agent@example.com")


def _agent_git(root: Path, *args: str) -> None:
    """Run bare ``git`` the way an agent (or the user, before the run) does."""
    REAL_GIT.run_text([*_AGENT_IDENTITY, *args], cwd=root).check()


def _nested_repository_with_a_filter(root: Path, marker: Path) -> None:
    """``sub``, a repository whose own config defines a clean filter that leaves ``marker``.

    Its tracked file is left stat-dirty with unchanged content, so checking whether ``sub``
    is modified makes Git rehash the file through the filter.
    """
    nested = root / "sub"
    nested.mkdir()
    _agent_git(nested, "init", "-q")
    (nested / "f.t").write_text("x\n", encoding="utf-8")
    _agent_git(nested, "add", "f.t")
    _agent_git(nested, "commit", "-q", "-m", "nested")
    filter_program = _plant_program(root.parent / "filter", marker)
    _agent_git(nested, "config", "filter.agent.clean", f"{filter_program}; cat")
    (nested / ".gitattributes").write_text("*.t filter=agent\n", encoding="utf-8")
    os.utime(nested / "f.t", (0, 0))


_WORK_TREE_READS = {
    "stage_all": lambda repo, _base: repo.stage_all(["."]),
    "uncommitted_paths": lambda repo, _base: repo.uncommitted_paths(["."]),
    "tracked_changes_since_head": lambda repo, _base: repo.tracked_changes_since_head(["."]),
    "changed_since": lambda repo, base: repo.changed_since(base, ["."]),
    "worktree_matches": lambda repo, base: repo.worktree_matches(base, ["."], include_ignored=True),
}


@pytest.mark.parametrize("recorded", [False, True], ids=["new", "recorded-as-gitlink"])
@pytest.mark.parametrize("operation", sorted(_WORK_TREE_READS))
def test_a_filter_a_nested_repository_defines_never_runs(
    sandbox: ContractProject, tmp_path: Path, *, operation: str, recorded: bool
) -> None:
    base = sandbox.start({"a.txt": "1\n"})
    marker = tmp_path / "ran"
    _nested_repository_with_a_filter(sandbox.root, marker)
    if recorded:
        # Git records the repository as a gitlink, as a snapshot taken before this
        # check existed (or the user's own commit) would have.
        _agent_git(sandbox.root, "add", "-A")
        _agent_git(sandbox.root, "commit", "-q", "--allow-empty", "-m", "gitlink")
        base = _commit_of(sandbox.root)
        os.utime(sandbox.root / "sub" / "f.t", (0, 0))
        marker.unlink(missing_ok=True)

    with pytest.raises(NestedRepositoryError) as refused:
        _WORK_TREE_READS[operation](sandbox.repo, base)

    assert "sub/.git" in refused.value.paths
    assert not marker.exists()


def _commit_of(root: Path) -> str:
    return REAL_GIT.run_text(["rev-parse", "HEAD"], cwd=root).check().stdout.strip()


def test_a_patch_runs_no_textconv_that_agent_attributes_select(
    sandbox: ContractProject, tmp_path: Path
) -> None:
    base = sandbox.start({"a.t": "1\n"})
    marker = tmp_path / "ran"
    program = _plant_program(tmp_path / "textconv", marker)
    # The user's (or the parent repository's) config defines the driver; the agent's
    # attributes select it for a file it chose.
    with (sandbox.root / ".git" / "config").open("a", encoding="utf-8") as config:
        config.write(f'[diff "user"]\n\ttextconv = {program}\n')
    sandbox.write(".gitattributes", "*.t diff=user\n")
    sandbox.write("a.t", "2\n")
    head = sandbox.commit_all("edit")

    patch = sandbox.repo.diff_patch(base, head)

    assert "+2" in patch
    assert not marker.exists()
