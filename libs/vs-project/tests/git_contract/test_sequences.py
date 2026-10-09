"""GitRepository contract: random operation sequences, compared with the oracle.

Each example drives the implementation under test and the reference (the Git
CLI) through the same generated operations on two identical project
directories and requires the same observable result after every step, plus
invariants that must hold for any implementation on its own. Commit ids are
not compared, they depend on the clock; subjects, file contents, and index and
worktree status are.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import GitCommandError, GitRepository

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.support.git_contract import RepositoryFactory

_PATHS = ("a.txt", "b.txt", "dir/c.txt")
_TEXTS = ("one\n", "two\n", "three\n")
_BRANCHES = ("main", "side", "other")


@dataclass(frozen=True)
class _Operation:
    kind: Literal[
        "write",
        "delete",
        "stage",
        "stage_dir",
        "unstage",
        "commit",
        "commit_only",
        "reset_index",
        "restore",
        "clean",
        "switch",
        "switch_new",
    ]
    path: str = _PATHS[0]
    text: str = _TEXTS[0]
    ordinal: int = 0
    branch: str = _BRANCHES[0]


_OPERATIONS = st.builds(
    _Operation,
    kind=st.sampled_from(
        [
            "write",
            "write",
            "delete",
            "stage",
            "stage_dir",
            "unstage",
            "commit",
            "commit",
            "commit_only",
            "reset_index",
            "restore",
            "clean",
            "switch",
            "switch_new",
        ]
    ),
    path=st.sampled_from(_PATHS),
    text=st.sampled_from(_TEXTS),
    ordinal=st.integers(min_value=0, max_value=3),
    branch=st.sampled_from(_BRANCHES),
)


def _write(repo: GitRepository, root: Path, operation: _Operation, step: int) -> None:
    del repo, step
    path = root / operation.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(operation.text, encoding="utf-8")


def _delete(repo: GitRepository, root: Path, operation: _Operation, step: int) -> None:
    del repo, step
    (root / operation.path).unlink(missing_ok=True)


def _restore(repo: GitRepository, root: Path, operation: _Operation, step: int) -> None:
    del root, step
    history = repo.recent_subjects(10)
    repo.restore_worktree(history[operation.ordinal % len(history)].sha)


def _clean(repo: GitRepository, root: Path, operation: _Operation, step: int) -> None:
    del root, operation, step
    repo.clean_untracked(include_ignored=False, protect="protected/")


_EFFECTS: dict[str, Callable[[GitRepository, Path, _Operation, int], None]] = {
    "write": _write,
    "delete": _delete,
    "stage": lambda repo, _root, _op, _step: repo.stage_all(["."]),
    "stage_dir": lambda repo, _root, _op, _step: repo.stage_all(["dir"]),
    "unstage": lambda repo, _root, op, _step: repo.unstage([op.path]),
    "commit": lambda repo, _root, _op, step: repo.commit(f"step {step}"),
    "commit_only": lambda repo, _root, op, step: repo.commit(f"only {step}", only=[op.path]),
    "reset_index": lambda repo, _root, _op, _step: repo.reset_index(),
    "restore": _restore,
    "clean": _clean,
    "switch": lambda repo, _root, op, _step: repo.switch_branch(op.branch),
    "switch_new": lambda repo, _root, op, _step: repo.switch_branch(op.branch, create=True),
}


def _apply(repo: GitRepository, root: Path, operation: _Operation, step: int) -> bool:
    """Apply one operation; ``False`` when the repository refused it."""
    try:
        _EFFECTS[operation.kind](repo, root, operation, step)
    except GitCommandError:
        return False
    return True


def _observe(repo: GitRepository) -> tuple[object, ...]:
    """Everything an implementation promises to expose, without clock-dependent ids."""
    return (
        repo.current_branch(),
        repo.head() is None,
        tuple(entry.subject for entry in repo.recent_subjects(50)) if repo.head() else (),
        tuple(repo.read_blob("HEAD", path) for path in _PATHS),
        repo.has_staged_changes(),
        repo.uncommitted_paths(["."]),
    )


# Each step spawns Git processes in the CLI implementation, so the example count is
# fixed small instead of following the Hypothesis profile.
@settings(max_examples=8)
@given(operations=st.lists(_OPERATIONS, min_size=1, max_size=7))
def test_sequences_agree_with_the_oracle_and_keep_their_invariants(
    factory: RepositoryFactory,
    oracle_factory: RepositoryFactory,
    operations: list[_Operation],
) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        roots = (Path(scratch) / "subject", Path(scratch) / "oracle")
        repos = []
        for root, make in zip(roots, (factory, oracle_factory), strict=True):
            root.mkdir()
            repo = make(root)
            repo.initialize(initial_branch="main")
            repo.bind()
            (root / "a.txt").write_text("base\n", encoding="utf-8")
            repo.stage_all(["."])
            repo.commit("baseline")
            repos.append(repo)
        subject, oracle = repos

        for step, operation in enumerate(operations):
            head_before = subject.head()
            staged_before = subject.has_staged_changes()
            accepted = _apply(subject, roots[0], operation, step)
            assert _apply(oracle, roots[1], operation, step) == accepted, operation
            assert _observe(subject) == _observe(oracle), operation

            head_after = subject.head()
            if operation.kind not in {"commit", "commit_only", "switch", "switch_new"}:
                assert head_after == head_before, operation
            if operation.kind == "commit":
                # A plain commit lands exactly when something was staged.
                assert accepted == staged_before
                assert (head_after != head_before) == accepted
                assert head_before is not None
                assert subject.is_ancestor(head_before, "HEAD")
                assert not subject.has_staged_changes()
            if operation.kind == "reset_index":
                assert not subject.has_staged_changes()
