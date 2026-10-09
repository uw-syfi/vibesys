"""GitRepository contract: staging, committing, and what survives a restart."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from vs_project.api import GitCommandError, StagingError

if TYPE_CHECKING:
    from tests.support.git_contract import Sandbox


def test_staged_changes_are_reported_until_committed(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo
    assert not repo.has_staged_changes()

    sandbox.write("a.txt", "2\n")
    assert not repo.has_staged_changes()
    repo.stage_all(["."])
    assert repo.has_staged_changes()
    assert repo.has_staged_changes(["a.txt"])
    assert not repo.has_staged_changes(["other.txt"])

    repo.commit("update a")
    assert not repo.has_staged_changes()
    assert repo.read_blob("HEAD", "a.txt") == b"2\n"


def test_stage_all_records_additions_modifications_and_deletions(sandbox: Sandbox) -> None:
    sandbox.start({"keep.txt": "k\n", "gone.txt": "g\n", "edit.txt": "1\n"})
    sandbox.delete("gone.txt")
    sandbox.write("edit.txt", "2\n")
    sandbox.write("new/deep/file.txt", "n\n")

    sandbox.commit_all("mixed changes")
    repo = sandbox.repo

    assert repo.read_blob("HEAD", "gone.txt") is None
    assert repo.read_blob("HEAD", "edit.txt") == b"2\n"
    assert repo.read_blob("HEAD", "new/deep/file.txt") == b"n\n"
    assert repo.read_blob("HEAD", "keep.txt") == b"k\n"


def test_stage_all_is_limited_to_its_pathspecs(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n", "dir/b.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    sandbox.write("dir/b.txt", "2\n")

    sandbox.repo.stage_all(["dir"])

    assert sandbox.repo.has_staged_changes(["dir"])
    assert not sandbox.repo.has_staged_changes(["a.txt"])


def test_ignored_files_are_staged_only_when_forced(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo
    assert repo.add_excludes(["*.log"]) == ("*.log",)
    sandbox.write("run.log", "noise\n")

    repo.stage_all(["."])
    assert not repo.has_staged_changes()

    repo.stage_all(["run.log"], force=True)
    assert repo.has_staged_changes(["run.log"])


def test_exclude_pathspecs_subtract_from_the_selection(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    sandbox.write("keep.txt", "k\n")
    sandbox.write("vendor/lib.txt", "v\n")

    sandbox.repo.stage_all([".", ":(exclude)vendor"])

    assert sandbox.repo.has_staged_changes(["keep.txt"])
    assert not sandbox.repo.has_staged_changes(["vendor"])


def test_unstage_resets_the_index_and_leaves_the_worktree(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo
    sandbox.write("a.txt", "2\n")
    sandbox.write("b.txt", "b\n")
    repo.stage_all(["."])

    repo.unstage(["a.txt"])

    assert not repo.has_staged_changes(["a.txt"])
    assert repo.has_staged_changes(["b.txt"])
    assert (sandbox.root / "a.txt").read_text(encoding="utf-8") == "2\n"
    repo.unstage(["."])
    assert not repo.has_staged_changes()
    assert (sandbox.root / "b.txt").exists()


def test_unstage_before_the_first_commit_empties_the_index(sandbox: Sandbox) -> None:
    sandbox.repo.initialize(initial_branch="main")
    sandbox.repo.bind()
    sandbox.write("a.txt", "1\n")
    sandbox.write("keep/b.txt", "b\n")
    sandbox.repo.stage_all(["."])

    sandbox.repo.unstage(["a.txt"])

    assert sandbox.repo.has_tracked_files("keep")
    assert not sandbox.repo.has_tracked_files("a.txt")
    assert (sandbox.root / "a.txt").exists()


def test_commit_without_staged_changes_is_refused_unless_empty_is_allowed(
    sandbox: Sandbox,
) -> None:
    baseline = sandbox.start({"a.txt": "1\n"})

    with pytest.raises(GitCommandError):
        sandbox.repo.commit("nothing to record")
    assert sandbox.repo.head() == baseline

    sandbox.repo.commit("marker", allow_empty=True)
    assert sandbox.repo.head() != baseline
    assert sandbox.repo.recent_subjects(1)[0].subject == "marker"


def test_commit_only_refuses_a_tracked_path_that_became_a_directory(sandbox: Sandbox) -> None:
    baseline = sandbox.start({"a.txt": "1\n"})
    sandbox.delete("a.txt")
    sandbox.write("a.txt/inner.txt", "inner\n")

    with pytest.raises(GitCommandError):
        sandbox.repo.commit("only", only=["."])

    assert sandbox.repo.head() == baseline


def test_commit_only_takes_the_named_paths_and_keeps_other_staged_changes(
    sandbox: Sandbox,
) -> None:
    sandbox.start({"a.txt": "1\n", "b.txt": "1\n"})
    repo = sandbox.repo
    sandbox.write("a.txt", "2\n")
    sandbox.write("b.txt", "2\n")
    repo.stage_all(["."])

    repo.commit("only a", only=["a.txt"])

    assert repo.read_blob("HEAD", "a.txt") == b"2\n"
    assert repo.read_blob("HEAD", "b.txt") == b"1\n"
    assert repo.has_staged_changes(["b.txt"])
    assert not repo.has_staged_changes(["a.txt"])


def test_commit_message_is_kept_verbatim(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")

    sandbox.commit_all("vibesys(round 3): record result [x] -- ok")

    assert sandbox.repo.recent_subjects(1)[0].subject == "vibesys(round 3): record result [x] -- ok"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_staging_names_unreadable_paths_and_stages_nothing(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo
    sandbox.write("a.txt", "2\n")
    secret = sandbox.write("profile.json", "{}\n")
    secret.chmod(0o000)
    try:
        with pytest.raises(StagingError) as raised:
            repo.stage_all(["."])

        assert "profile.json" in raised.value.unreadable
        assert not repo.has_staged_changes()

        assert repo.add_excludes(["/profile.json"]) == ("/profile.json",)
        repo.stage_all(["."])
        assert repo.has_staged_changes(["a.txt"])
        assert not repo.has_staged_changes(["profile.json"])
    finally:
        secret.chmod(0o644)


def test_other_staging_failures_name_no_unreadable_paths(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})

    with pytest.raises(StagingError) as raised:
        sandbox.repo.stage_all(["does-not-exist.txt"])

    assert raised.value.unreadable == ()


def test_excludes_are_added_once_in_order(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo

    assert repo.add_excludes(["/x/", "*.tmp", "/x/"]) == ("/x/", "*.tmp")
    assert repo.add_excludes(["*.tmp", "/y"]) == ("/y",)
    assert repo.add_excludes(["/y"]) == ()
    sandbox.write("x/f.txt", "f\n")
    sandbox.write("z.tmp", "t\n")
    assert repo.uncommitted_paths(["."]) == ()


# -- what a restart sees: the checkpoint recovery contract --------------------------


def test_a_new_instance_sees_staged_state_branches_and_commits(sandbox: Sandbox) -> None:
    baseline = sandbox.start({"a.txt": "1\n"})
    sandbox.repo.switch_branch("run", create=True)
    sandbox.write("a.txt", "2\n")
    sandbox.repo.stage_all(["."])

    restarted = sandbox.reopen()

    assert restarted.head() == baseline
    assert restarted.current_branch() == "run"
    assert restarted.has_staged_changes()
    restarted.commit("after restart")
    assert sandbox.repo.head() == restarted.head()
    assert sandbox.repo.head() != baseline


def test_recovery_tells_a_landed_commit_from_a_lost_one_by_head_alone(sandbox: Sandbox) -> None:
    """The checkpoint journal records ``HEAD`` before committing.

    After a crash, a fresh instance decides from ``head()`` and ``is_ancestor``
    only: ``HEAD`` equal to the journaled commit means the commit did not land,
    a descendant means it did, and nothing else is ever observable.
    """
    pre_commit = sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    sandbox.repo.stage_all(["."])

    # Crash before the commit: the staged change alone does not move HEAD.
    before = sandbox.reopen()
    assert before.head() == pre_commit
    assert before.is_ancestor(pre_commit, "HEAD")

    # A refused commit is a lost commit.
    with pytest.raises(GitCommandError):
        sandbox.repo.commit("refused", only=["missing.txt"])
    assert sandbox.reopen().head() == pre_commit

    # Crash after the commit: the new HEAD descends from the journaled commit.
    sandbox.repo.commit("landed")
    after = sandbox.reopen()
    landed = after.head()
    assert landed is not None
    assert landed != pre_commit
    assert after.is_ancestor(pre_commit, "HEAD")
    assert not after.is_ancestor(landed, pre_commit)


def test_a_failed_operation_leaves_head_and_index_as_they_were(sandbox: Sandbox) -> None:
    baseline = sandbox.start({"a.txt": "1\n", "b.txt": "1\n"})
    repo = sandbox.repo
    sandbox.write("a.txt", "2\n")
    repo.stage_all(["a.txt"])

    with pytest.raises(GitCommandError):
        repo.update_ref("refs/vibesys/run/candidates/x", "1" * 40)
    with pytest.raises(GitCommandError):
        repo.switch_branch("main", create=True)
    with pytest.raises(GitCommandError):
        repo.restore_worktree("1" * 40)

    assert repo.head() == baseline
    assert repo.has_staged_changes(["a.txt"])
    assert not repo.has_staged_changes(["b.txt"])
