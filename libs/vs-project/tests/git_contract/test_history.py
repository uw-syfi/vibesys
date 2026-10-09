"""GitRepository contract: location, names, and reading history."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import GitCommandError

if TYPE_CHECKING:
    from pathlib import Path

    from tests.support.git_contract import RepositoryFactory, Sandbox

_FULL_ID = re.compile(r"^[0-9a-f]{40}$")


def test_a_directory_becomes_a_repository_with_an_unborn_head(sandbox: Sandbox) -> None:
    repo = sandbox.repo
    assert not repo.is_inside_work_tree()

    repo.initialize(initial_branch="trunk")
    location = repo.bind()

    assert repo.is_inside_work_tree()
    assert repo.toplevel() == sandbox.root.resolve()
    assert location.work_tree == sandbox.root.resolve()
    assert location.git_dir.is_dir()
    assert repo.head() is None
    assert repo.current_branch() == "trunk"
    assert not repo.branch_exists("trunk")


def test_toplevel_outside_a_repository_is_a_command_error(sandbox: Sandbox) -> None:
    with pytest.raises(GitCommandError):
        sandbox.repo.toplevel()


@pytest.mark.parametrize("name", ["a", "feat/x", "vibesys-runs/run-1", "v1.2", "a_b"])
def test_legal_names_are_accepted(sandbox: Sandbox, name: str) -> None:
    assert sandbox.repo.is_valid_branch_name(name)
    assert sandbox.repo.is_valid_ref_name(f"refs/vibesys/{name}")


@pytest.mark.parametrize(
    "name", ["", "a..b", "a.lock", "a.", "a b", "a~1", "/a", "a//b", "a:b", "a@{b", "a\\b"]
)
def test_illegal_names_are_rejected(sandbox: Sandbox, name: str) -> None:
    assert not sandbox.repo.is_valid_branch_name(name)
    assert not sandbox.repo.is_valid_ref_name(f"refs/vibesys/{name}")


def test_a_branch_name_cannot_look_like_an_option(sandbox: Sandbox) -> None:
    assert not sandbox.repo.is_valid_branch_name("-x")


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(name=st.text(alphabet="ab./-_@{~^: *?[\\", max_size=8))
def test_name_validity_agrees_with_the_oracle(
    sandbox: Sandbox, oracle_factory: RepositoryFactory, name: str
) -> None:
    # Name checks read nothing from the directory, so one sandbox serves every example.
    oracle = oracle_factory(sandbox.root)
    repo = sandbox.repo
    assert repo.is_valid_branch_name(name) == oracle.is_valid_branch_name(name)
    assert repo.is_valid_ref_name(f"refs/x/{name}") == oracle.is_valid_ref_name(f"refs/x/{name}")


def test_head_follows_commits_and_names_a_full_id(sandbox: Sandbox) -> None:
    first = sandbox.start({"a.txt": "1\n"})
    assert _FULL_ID.match(first)
    sandbox.write("a.txt", "2\n")
    second = sandbox.commit_all("second")

    assert second != first
    assert sandbox.repo.head() == second
    assert sandbox.repo.current_branch() == "main"
    assert sandbox.repo.branch_exists("main")


def test_head_is_visible_to_a_new_instance_after_a_commit(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    committed = sandbox.commit_all("second")

    assert sandbox.reopen().head() == committed


def test_resolve_commit_expands_abbreviations_and_rejects_unknowns(sandbox: Sandbox) -> None:
    head = sandbox.start({"a.txt": "1\n"})
    repo = sandbox.repo

    assert repo.resolve_commit(head) == head
    assert repo.resolve_commit(head[:10]) == head
    assert repo.resolve_commit("HEAD") == head
    assert repo.resolve_commit("0" * 40) is None
    assert repo.resolve_commit("not-a-revision") is None


def test_ancestry_follows_history_and_includes_the_commit_itself(sandbox: Sandbox) -> None:
    first = sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    second = sandbox.commit_all("second")
    repo = sandbox.repo

    assert repo.is_ancestor(first, second)
    assert repo.is_ancestor(first, "HEAD")
    assert repo.is_ancestor(second, second)
    assert not repo.is_ancestor(second, first)
    assert not repo.is_ancestor("0" * 40, second)


def test_root_commit_is_the_oldest_parentless_commit(sandbox: Sandbox) -> None:
    first = sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    second = sandbox.commit_all("second")

    assert sandbox.repo.root_commit(second) == first
    assert sandbox.repo.root_commit(first) == first
    with pytest.raises(GitCommandError):
        sandbox.repo.root_commit("0" * 40)


def test_recent_subjects_are_newest_first_and_bounded(sandbox: Sandbox) -> None:
    first = sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    second = sandbox.commit_all("second: with detail")
    sandbox.write("a.txt", "3\n")
    third = sandbox.commit_all("third")

    subjects = sandbox.repo.recent_subjects(2)

    assert [(entry.sha, entry.subject) for entry in subjects] == [
        (third, "third"),
        (second, "second: with detail"),
    ]
    assert [entry.sha for entry in sandbox.repo.recent_subjects(10)] == [third, second, first]


def test_recent_subjects_of_an_unborn_head_is_a_command_error(sandbox: Sandbox) -> None:
    sandbox.repo.initialize(initial_branch="main")
    with pytest.raises(GitCommandError):
        sandbox.repo.recent_subjects(5)


def test_blobs_are_read_by_revision_and_absent_paths_are_none(sandbox: Sandbox) -> None:
    first = sandbox.start({"a.txt": "1\n", "dir/b.bin": "b\n"})
    sandbox.write("a.txt", "2\n")
    sandbox.commit_all("second")
    repo = sandbox.repo

    assert repo.read_blob("HEAD", "a.txt") == b"2\n"
    assert repo.read_blob(first, "a.txt") == b"1\n"
    assert repo.read_blob("HEAD", "dir/b.bin") == b"b\n"
    assert repo.read_blob("HEAD", "missing.txt") is None
    assert repo.read_blob("0" * 40, "a.txt") is None


def test_refs_hold_commits_and_report_what_they_reach(sandbox: Sandbox) -> None:
    first = sandbox.start({"a.txt": "1\n"})
    sandbox.write("a.txt", "2\n")
    second = sandbox.commit_all("second")
    sandbox.write("a.txt", "3\n")
    third = sandbox.commit_all("third")
    repo = sandbox.repo
    prefix = "refs/vibesys/run/candidates/"

    assert not repo.has_ref_containing(first, prefix)
    repo.update_ref(f"{prefix}cand-1", second)

    assert repo.has_ref_containing(first, prefix)
    assert repo.has_ref_containing(second, prefix)
    assert not repo.has_ref_containing(third, prefix)
    assert not repo.has_ref_containing(first, "refs/vibesys/other/")
    repo.update_ref(f"{prefix}cand-1", third)
    assert repo.has_ref_containing(third, prefix)


def test_update_ref_rejects_a_commit_that_does_not_exist(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    with pytest.raises(GitCommandError):
        sandbox.repo.update_ref("refs/vibesys/run/candidates/x", "1" * 40)


def test_first_commit_adding_finds_the_earliest_addition(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n"})
    sandbox.write("keep.py", "1\n")
    added = sandbox.commit_all("add keep")
    sandbox.write("keep.py", "2\n")
    sandbox.commit_all("modify keep")
    repo = sandbox.repo

    assert repo.first_commit_adding(["keep.py"]) == added
    assert repo.first_commit_adding([":(literal)keep.py"]) == added
    assert repo.first_commit_adding(["never.py"]) is None


def test_reachable_paths_include_deleted_paths_and_other_branches(sandbox: Sandbox) -> None:
    sandbox.start({"a.txt": "1\n", "secret/.env": "KEY=1\n"})
    sandbox.delete("secret/.env")
    sandbox.commit_all("remove the secret")
    sandbox.repo.switch_branch("side", create=True)
    sandbox.write("side-only.txt", "x\n")
    sandbox.commit_all("side work")
    sandbox.repo.switch_branch("main")

    paths = sandbox.repo.reachable_paths()

    assert {"a.txt", "secret/.env", "side-only.txt"} <= paths


def test_switching_branches_moves_head_and_refuses_over_conflicting_changes(
    sandbox: Sandbox,
) -> None:
    base = sandbox.start({"f.txt": "base\n"})
    repo = sandbox.repo
    repo.switch_branch("other", create=True)
    sandbox.write("f.txt", "other\n")
    other = sandbox.commit_all("other change")
    assert repo.current_branch() == "other"
    assert repo.head() == other

    repo.switch_branch("main")
    assert repo.head() == base
    sandbox.write("f.txt", "uncommitted\n")
    with pytest.raises(GitCommandError):
        repo.switch_branch("other")

    assert repo.current_branch() == "main"
    assert repo.head() == base
    assert (sandbox.root / "f.txt").read_text(encoding="utf-8") == "uncommitted\n"


def test_creating_an_existing_branch_is_a_command_error(sandbox: Sandbox) -> None:
    sandbox.start({"f.txt": "base\n"})
    with pytest.raises(GitCommandError):
        sandbox.repo.switch_branch("main", create=True)


def test_detached_head_has_no_branch(sandbox: Sandbox, tmp_path: Path) -> None:
    head = sandbox.start({"f.txt": "base\n"})
    destination = tmp_path / "linked"

    sandbox.repo.add_worktree(destination, head)

    assert sandbox.factory(destination).current_branch() is None
