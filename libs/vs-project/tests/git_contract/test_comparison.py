"""GitRepository contract: diffs, status, and worktree-versus-revision comparison."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from vs_project.api import GitCommandError, PatchStyle

if TYPE_CHECKING:
    from vs_project.api.testing import ContractProject

_BODY = "".join(f"line {number}\n" for number in range(20))


def _renamed(sandbox: ContractProject) -> tuple[str, str]:
    base = sandbox.start({"old.txt": _BODY, "stay.txt": "s\n"})
    (sandbox.root / "old.txt").rename(sandbox.root / "new.txt")
    return base, sandbox.commit_all("rename")


def test_review_patches_show_renames_and_exact_patches_do_not(sandbox: ContractProject) -> None:
    base, renamed = _renamed(sandbox)

    review = sandbox.repo.diff_patch(base, renamed)
    exact = sandbox.repo.diff_patch(base, renamed, style=PatchStyle.EXACT)

    assert "rename from old.txt" in review
    assert "rename from" not in exact
    assert "deleted file mode" in exact
    assert "new file mode" in exact
    ids = re.findall(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", exact, flags=re.MULTILINE)
    assert ids
    assert all(len(left) == len(right) == 40 for left, right in ids)


def test_patches_are_limited_to_pathspecs(sandbox: ContractProject) -> None:
    base = sandbox.start({"a.txt": "1\n", "dir/b.txt": "1\n", "dir/skip.txt": "1\n"})
    for path in ("a.txt", "dir/b.txt", "dir/skip.txt"):
        sandbox.write(path, "2\n")
    head = sandbox.commit_all("edit all")
    repo = sandbox.repo

    only_a = repo.diff_patch(base, head, [":(literal)a.txt"])
    assert "a/a.txt" in only_a
    assert "dir/" not in only_a

    without_skip = repo.diff_patch(base, head, [".", ":(exclude)dir/skip.txt"])
    assert "a/dir/b.txt" in without_skip
    assert "skip.txt" not in without_skip

    assert repo.diff_patch(base, base) == ""


def test_diffs_between_unknown_revisions_are_command_errors(sandbox: ContractProject) -> None:
    head = sandbox.start({"a.txt": "1\n"})

    with pytest.raises(GitCommandError):
        sandbox.repo.diff_patch("1" * 40, head)
    with pytest.raises(GitCommandError):
        sandbox.repo.diff_name_status(head, "1" * 40)


def test_name_status_is_nul_delimited_with_rename_detection(sandbox: ContractProject) -> None:
    base, renamed = _renamed(sandbox)
    sandbox.write("added.txt", "a\n")
    sandbox.delete("stay.txt")
    final = sandbox.commit_all("add and delete")

    renames = sandbox.repo.diff_name_status(base, renamed).split("\0")
    assert renames[:3] == ["R100", "old.txt", "new.txt"]

    changes = sandbox.repo.diff_name_status(renamed, final).split("\0")
    records = {changes[index + 1]: changes[index] for index in range(0, len(changes) - 1, 2)}
    assert records == {"added.txt": "A", "stay.txt": "D"}


def test_tracked_changes_since_head_counts_index_and_worktree_but_not_untracked(
    sandbox: ContractProject,
) -> None:
    sandbox.start({"a.txt": "1\n", "b.txt": "1\n", "c/d.txt": "1\n"})
    repo = sandbox.repo
    assert repo.tracked_changes_since_head(["."]) == ()

    sandbox.write("b.txt", "2\n")
    sandbox.write("c/d.txt", "2\n")
    sandbox.write("untracked.txt", "u\n")
    sandbox.write("staged.txt", "s\n")
    repo.stage_all(["staged.txt"])

    assert repo.tracked_changes_since_head(["."]) == ("b.txt", "c/d.txt", "staged.txt")
    assert repo.tracked_changes_since_head(["c"]) == ("c/d.txt",)
    assert repo.tracked_changes_since_head([":(literal)b.txt"]) == ("b.txt",)


def test_uncommitted_paths_list_modified_staged_and_untracked_files_individually(
    sandbox: ContractProject,
) -> None:
    sandbox.start({"a.txt": "1\n", "b.txt": "1\n"})
    repo = sandbox.repo
    assert repo.uncommitted_paths(["."]) == ()

    sandbox.write("a.txt", "2\n")
    sandbox.write("fresh/one.txt", "1\n")
    sandbox.write("fresh/two.txt", "2\n")
    sandbox.write("staged.txt", "s\n")
    repo.stage_all(["staged.txt"])
    repo.add_excludes(["*.log"])
    sandbox.write("ignored.log", "x\n")

    assert repo.uncommitted_paths(["."]) == (
        "a.txt",
        "fresh/one.txt",
        "fresh/two.txt",
        "staged.txt",
    )
    assert repo.uncommitted_paths(["fresh"]) == ("fresh/one.txt", "fresh/two.txt")


def test_changed_since_unions_committed_and_uncommitted_paths(sandbox: ContractProject) -> None:
    base = sandbox.start({"a.txt": "1\n", "b.txt": "1\n", "tests/t.py": "1\n"})
    sandbox.write("a.txt", "2\n")
    sandbox.write("tests/t.py", "2\n")
    sandbox.commit_all("committed edits")
    sandbox.write("b.txt", "2\n")
    sandbox.write("tests/new.py", "n\n")
    repo = sandbox.repo

    assert repo.changed_since(base, ["."]) == ("a.txt", "b.txt", "tests/new.py", "tests/t.py")
    assert repo.changed_since(base, ["tests"]) == ("tests/new.py", "tests/t.py")
    assert repo.changed_since("HEAD", ["tests"]) == ("tests/new.py",)
    with pytest.raises(GitCommandError):
        repo.changed_since("1" * 40, ["."])


def test_tracked_file_queries(sandbox: ContractProject) -> None:
    sandbox.start({"dir/a.txt": "1\n"})
    sandbox.write("loose.txt", "x\n")

    assert sandbox.repo.has_tracked_files("dir")
    assert sandbox.repo.has_tracked_files("dir/a.txt")
    assert not sandbox.repo.has_tracked_files("loose.txt")
    assert not sandbox.repo.has_tracked_files("missing")


def test_worktree_matches_compares_tracked_untracked_and_optionally_ignored(
    sandbox: ContractProject,
) -> None:
    head = sandbox.start({"a.txt": "1\n", "dir/b.txt": "1\n"})
    repo = sandbox.repo
    repo.add_excludes(["*.log"])
    assert repo.worktree_matches(head, ["."], include_ignored=False)

    sandbox.write("dir/b.txt", "2\n")
    assert not repo.worktree_matches(head, ["."], include_ignored=False)
    assert repo.worktree_matches(head, [".", ":(exclude)dir"], include_ignored=False)
    sandbox.write("dir/b.txt", "1\n")

    sandbox.write("extra.txt", "x\n")
    assert not repo.worktree_matches(head, ["."], include_ignored=False)
    (sandbox.root / "extra.txt").unlink()

    sandbox.write("noise.log", "n\n")
    assert repo.worktree_matches(head, ["."], include_ignored=False)
    assert not repo.worktree_matches(head, ["."], include_ignored=True)

    (sandbox.root / "a.txt").unlink()
    assert not repo.worktree_matches(head, ["."], include_ignored=False)


def test_worktree_matches_leaves_the_real_index_alone(sandbox: ContractProject) -> None:
    head = sandbox.start({"a.txt": "1\n"})
    sandbox.write("new.txt", "n\n")

    sandbox.repo.worktree_matches(head, ["."], include_ignored=False)

    assert not sandbox.repo.has_staged_changes()
    assert sandbox.repo.uncommitted_paths(["."]) == ("new.txt",)


def test_worktree_matches_an_unknown_revision_is_false(sandbox: ContractProject) -> None:
    sandbox.start({"a.txt": "1\n"})

    assert not sandbox.repo.worktree_matches("1" * 40, ["."], include_ignored=False)
