"""GitRepository contract: no operation reads a work tree that holds a nested repository.

Git checks a nested repository by running a child Git inside it, under that
repository's agent-written config, so every operation that reads the work tree refuses
with ``NestedRepositoryError`` while a ``.git`` (a directory, a file or a symlink) is
anywhere below the top level, and works again once it is gone.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import NestedRepositoryError
from vs_project.api.testing import ContractProject
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_project.api import GitRepository
    from vs_project.api.testing import RepositoryFactory

type _Operation = Callable[[GitRepository, str], object]

_OPERATIONS: dict[str, _Operation] = {
    "stage_all": lambda repo, _base: repo.stage_all(["."]),
    "uncommitted_paths": lambda repo, _base: repo.uncommitted_paths(["."]),
    "tracked_changes_since_head": lambda repo, _base: repo.tracked_changes_since_head(["."]),
    "changed_since": lambda repo, base: repo.changed_since(base, ["."]),
    "worktree_matches": lambda repo, base: repo.worktree_matches(base, ["."], include_ignored=True),
    "commit_only": lambda repo, _base: repo.commit("partial", only=["a.txt"]),
}


def _plant(root: Path, relative: str, kind: str) -> None:
    """A ``.git`` of ``kind`` at ``relative``, as an agent's ``git init`` or clone leaves."""
    nested = root / relative
    nested.parent.mkdir(parents=True, exist_ok=True)
    match kind:
        case "directory":
            nested.mkdir()
            (nested / "config").write_text("[core]\n", encoding="utf-8")
        case "file":
            nested.write_text(f"gitdir: {root.parent / 'elsewhere'}\n", encoding="utf-8")
        case _:
            nested.symlink_to(root.parent / "elsewhere")


_DIRECTORIES = st.lists(st.sampled_from(["src", "deep", "repos", ".hidden"]), max_size=3)


@settings(max_examples=examples(pr=2, nightly=25), deadline=None)
@given(
    directories=_DIRECTORIES.filter(bool),
    kind=st.sampled_from(["directory", "file", "symlink"]),
    operation=st.sampled_from(sorted(_OPERATIONS)),
)
def test_each_work_tree_read_refuses_while_a_nested_repository_exists(
    factory: RepositoryFactory, directories: list[str], kind: str, operation: str
) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        sandbox = ContractProject(root=Path(scratch) / "project", factory=factory)
        sandbox.root.mkdir()
        base = sandbox.start({"a.txt": "1\n", "src/b.txt": "2\n"})
        sandbox.write("a.txt", "edited\n")
        nested = "/".join([*directories, ".git"])
        _plant(sandbox.root, nested, kind)

        with pytest.raises(NestedRepositoryError) as refused:
            _OPERATIONS[operation](sandbox.repo, base)

        assert refused.value.paths == (nested,)
        assert sandbox.repo.head() == base
        assert not sandbox.repo.has_staged_changes()
        if kind == "directory":
            shutil.rmtree(sandbox.root / nested)
        else:
            (sandbox.root / nested).unlink()
        _OPERATIONS[operation](sandbox.repo, base)


@pytest.mark.parametrize(
    ("nested", "refused"),
    [
        ("repos/x/.git", False),  # outside what the command reads
        ("src/inner/x/.git", True),  # inside it
        ("src/x/.git", False),  # beside it
        ("src/.git", True),  # the selected directory is itself a repository
        (".git/modules/x/.git", False),  # inside the pinned repository
    ],
)
def test_a_command_scoped_by_pathspecs_refuses_only_a_repository_it_would_read(
    sandbox: ContractProject, *, nested: str, refused: bool
) -> None:
    sandbox.start({"a.txt": "1\n", "src/inner/b.txt": "2\n"})
    sandbox.write("src/inner/b.txt", "edited\n")
    _plant(sandbox.root, nested, "directory")

    if refused:
        with pytest.raises(NestedRepositoryError):
            sandbox.repo.stage_all(["src/inner"])
    else:
        sandbox.repo.stage_all(["src/inner"])
        assert sandbox.repo.has_staged_changes(["src/inner"])


def test_a_linked_worktree_inside_the_work_tree_is_not_a_nested_repository(
    sandbox: ContractProject,
) -> None:
    base = sandbox.start({"a.txt": "1\n", ".gitignore": "/state/\n"})
    sandbox.repo.add_worktree(sandbox.root / "state" / "candidate", base)
    sandbox.write("a.txt", "edited\n")

    assert sandbox.repo.uncommitted_paths(["."]) == ("a.txt",)
    sandbox.repo.stage_all(["."])
    assert sandbox.repo.has_staged_changes()
