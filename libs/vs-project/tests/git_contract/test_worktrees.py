"""GitRepository contract: restoring the worktree and linked worktrees."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from vs_project.api import GitCommandError

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api.testing import ContractProject


def test_clean_keeps_files_under_a_directory_that_replaced_a_tracked_file(
    sandbox: ContractProject,
) -> None:
    sandbox.start({"a.txt": "1\n"})
    sandbox.delete("a.txt")
    sandbox.write("a.txt/inner.txt", "inner\n")

    assert sandbox.repo.clean_untracked(include_ignored=False, protect="protected/")

    assert (sandbox.root / "a.txt" / "inner.txt").exists()
    sandbox.repo.restore_worktree("HEAD")
    assert (sandbox.root / "a.txt").read_text(encoding="utf-8") == "1\n"


def test_reset_index_unstages_everything_and_keeps_head_and_files(sandbox: ContractProject) -> None:
    head = sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    sandbox.write("b.txt", "b\n")
    sandbox.repo.stage_all(["."])

    sandbox.repo.reset_index()

    assert sandbox.repo.head() == head
    assert not sandbox.repo.has_staged_changes()
    assert (sandbox.root / "a.txt").read_text(encoding="utf-8") == "2\n"
    assert (sandbox.root / "b.txt").exists()


def test_restore_worktree_rebuilds_tracked_files_without_moving_head_or_index(
    sandbox: ContractProject,
) -> None:
    first = sandbox.start({"a.txt": "1\n", "keep/cfg.txt": "cfg1\n"})
    sandbox.write("a.txt", "2\n")
    sandbox.write("later.txt", "l\n")
    sandbox.write("keep/cfg.txt", "cfg2\n")
    latest = sandbox.commit_all("later work")
    repo = sandbox.repo

    repo.restore_worktree(first, [":(exclude)keep", ":(exclude)keep/**"])

    assert repo.head() == latest
    assert (sandbox.root / "a.txt").read_text(encoding="utf-8") == "1\n"
    assert not (sandbox.root / "later.txt").exists()
    assert (sandbox.root / "keep/cfg.txt").read_text(encoding="utf-8") == "cfg2\n"
    assert not repo.has_staged_changes()

    repo.restore_worktree(first)
    assert (sandbox.root / "keep/cfg.txt").read_text(encoding="utf-8") == "cfg1\n"


def test_restore_of_an_unknown_revision_is_a_command_error(sandbox: ContractProject) -> None:
    sandbox.start({"a.txt": "1\n"})
    with pytest.raises(GitCommandError):
        sandbox.repo.restore_worktree("1" * 40)


def test_clean_removes_untracked_files_but_not_ignored_or_protected_ones(
    sandbox: ContractProject,
) -> None:
    sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo
    repo.add_excludes(["*.log"])
    sandbox.write("stray.txt", "s\n")
    sandbox.write("nested/stray.txt", "s\n")
    sandbox.write("noise.log", "n\n")
    sandbox.write("cfg/settings.txt", "c\n")

    assert repo.clean_untracked(include_ignored=False, protect="cfg/")

    assert not (sandbox.root / "stray.txt").exists()
    assert not (sandbox.root / "nested").exists()
    assert (sandbox.root / "noise.log").exists()
    assert (sandbox.root / "cfg/settings.txt").exists()
    assert (sandbox.root / "a.txt").exists()

    assert repo.clean_untracked(include_ignored=True, protect="cfg/")
    assert not (sandbox.root / "noise.log").exists()
    assert (sandbox.root / "cfg/settings.txt").exists()


def test_restore_then_clean_yields_exactly_the_revision(sandbox: ContractProject) -> None:
    first = sandbox.start({"a.txt": "1\n", "dir/b.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    sandbox.write("dir/c.txt", "c\n")
    sandbox.commit_all("grow")
    sandbox.write("debris.txt", "d\n")
    repo = sandbox.repo

    repo.reset_index()
    repo.clean_untracked(include_ignored=True, protect="protected-dir/")
    repo.restore_worktree(first)

    assert repo.worktree_matches(first, ["."], include_ignored=True)


def test_linked_worktrees_share_history_and_have_their_own_head(
    sandbox: ContractProject, tmp_path: Path
) -> None:
    base = sandbox.start({"a.txt": "1\n"})
    destination = tmp_path / "linked" / "candidate"
    destination.parent.mkdir()
    repo = sandbox.repo

    repo.add_worktree(destination, base)

    assert (destination / "a.txt").read_text(encoding="utf-8") == "1\n"
    assert repo.worktree_head(destination) == base

    linked = sandbox.factory(destination)
    (destination / "a.txt").write_text("edited in the candidate\n", encoding="utf-8")
    linked.stage_all(["."])
    linked.commit("candidate work")
    candidate = repo.worktree_head(destination)

    assert candidate != base
    assert repo.head() == base
    assert repo.resolve_commit(candidate) == candidate
    assert repo.is_ancestor(base, candidate)
    assert (sandbox.root / "a.txt").read_text(encoding="utf-8") == "1\n"


def test_a_removed_worktree_can_be_added_again_at_the_same_path(
    sandbox: ContractProject, tmp_path: Path
) -> None:
    base = sandbox.start({"a.txt": "1\n"})
    destination = tmp_path / "linked"
    repo = sandbox.repo
    repo.add_worktree(destination, base)

    repo.remove_worktree(destination)
    shutil.rmtree(destination, ignore_errors=True)
    repo.prune_worktrees()

    repo.add_worktree(destination, base)
    assert repo.worktree_head(destination) == base


def test_removal_and_pruning_tolerate_unknown_worktrees(
    sandbox: ContractProject, tmp_path: Path
) -> None:
    sandbox.start({"a.txt": "1\n"})

    sandbox.repo.remove_worktree(tmp_path / "never-added")
    sandbox.repo.prune_worktrees()


def test_adding_a_worktree_at_a_missing_commit_is_a_command_error(
    sandbox: ContractProject, tmp_path: Path
) -> None:
    sandbox.start({"a.txt": "1\n"})

    with pytest.raises(GitCommandError):
        sandbox.repo.add_worktree(tmp_path / "linked", "1" * 40)
    assert not (tmp_path / "linked" / "a.txt").exists()


def test_worktree_head_of_a_plain_directory_is_a_command_error(
    sandbox: ContractProject, tmp_path: Path
) -> None:
    sandbox.start({"a.txt": "1\n"})
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(GitCommandError):
        sandbox.repo.worktree_head(plain)
