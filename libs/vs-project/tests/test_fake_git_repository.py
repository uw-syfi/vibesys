"""What ``FakeGitRepositories`` adds to the ``GitRepository`` contract: failure knobs and worktree listing.

The contract itself (``tests/git_contract``) already runs against the Fake.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import GitCommandError, NullGitTrackerEvents, run_git
from vs_project.api.testing import FakeGitRepositories, RepositoryCensus


class _Warnings(NullGitTrackerEvents):
    def __init__(self) -> None:
        self.summaries: list[str] = []

    def warning(self, summary: str, *, detail: str | None = None) -> None:
        del detail
        self.summaries.append(summary)


def _started(disks: FakeGitRepositories, root: Path) -> str:
    repo = disks.repository(root)
    repo.initialize(initial_branch="main")
    repo.bind()
    (root / "a.txt").write_text("1\n", encoding="utf-8")
    repo.stage_all(["."])
    repo.commit("baseline")
    head = repo.head()
    assert head is not None
    return head


def test_a_queued_failure_is_raised_once_before_the_call_changes_anything(tmp_path: Path) -> None:
    disks = FakeGitRepositories()
    baseline = _started(disks, tmp_path)
    repo = disks.repository(tmp_path)
    (tmp_path / "a.txt").write_text("2\n", encoding="utf-8")
    repo.stage_all(["."])

    disks.fail_next("commit", GitCommandError(["git", "commit"], 1, "injected"))
    with pytest.raises(GitCommandError, match="injected"):
        repo.commit("lost")

    assert repo.head() == baseline
    assert repo.has_staged_changes()
    repo.commit("kept")
    assert repo.head() != baseline


def test_refused_commands_are_reported_to_the_fault_sink(tmp_path: Path) -> None:
    disks = FakeGitRepositories()
    _started(disks, tmp_path)
    warnings = _Warnings()
    repo = disks.repository(tmp_path, faults=warnings)

    with pytest.raises(GitCommandError):
        repo.commit("nothing staged")

    assert warnings.summaries == ["git command failed: git commit -m nothing staged"]


def test_worktrees_lists_the_main_checkout_first_then_linked_ones(tmp_path: Path) -> None:
    disks = FakeGitRepositories()
    project = tmp_path / "project"
    project.mkdir()
    base = _started(disks, project)
    linked = tmp_path / "linked"

    disks.repository(project).add_worktree(linked, base)

    assert disks.worktrees(project) == (project.resolve(), linked.resolve())
    assert disks.worktrees(linked) == disks.worktrees(project)
    disks.repository(project).remove_worktree(linked)
    assert disks.worktrees(project) == (project.resolve(),)


def test_the_census_counts_what_a_repository_holds(tmp_path: Path) -> None:
    disks = FakeGitRepositories()
    project = tmp_path / "project"
    project.mkdir()
    base = _started(disks, project)
    repo = disks.repository(project)
    assert disks.census(project) == RepositoryCensus(worktrees=1, commits=1, refs=1)

    linked = tmp_path / "linked"
    repo.add_worktree(linked, base)
    (linked / "a.txt").write_text("2\n", encoding="utf-8")
    candidate = disks.repository(linked)
    candidate.stage_all(["."])
    candidate.commit("candidate")
    repo.update_ref("refs/vibesys/run/candidates/c1", candidate.head() or "")

    assert disks.census(project) == RepositoryCensus(worktrees=2, commits=2, refs=2)


@settings(max_examples=8, deadline=None)
@given(
    files=st.dictionaries(
        st.sampled_from(["a.txt", "dir/b.txt", "dir/sub/c.py", "z", "dir.txt", "dir/a b"]),
        st.binary(max_size=40),
        min_size=1,
    )
)
def test_tree_ids_are_the_ids_real_git_computes(files: dict[str, bytes]) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        ours, theirs = Path(scratch, "ours"), Path(scratch, "theirs")
        for root in (ours, theirs):
            for name, data in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(data)
        fake = FakeGitRepositories().repository(ours)
        fake.initialize(initial_branch="main")
        fake.stage_all(["."])
        fake.commit("tree")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"]):
            assert run_git(args, cwd=theirs).returncode == 0
        written = run_git(["write-tree"], cwd=theirs).stdout.decode().strip()

        assert fake.tree_of("HEAD") == written
