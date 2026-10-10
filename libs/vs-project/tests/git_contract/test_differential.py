"""GitRepository contract: wide random operation sequences, compared with the oracle.

``test_sequences`` covers the operations a checkpoint uses most. This module drives the
whole mutating surface (ignore rules, pathspecs of every supported kind, partial
commits, branch switches over local changes, file/directory replacement, executable
bits) on two identical project directories, one served by the implementation under test
and one by the reference, and requires that after every step both

* agree on whether the operation was refused, and on the kind of refusal,
* answer every read operation the same way, and
* leave the same files on disk.

Commit ids are not compared; they depend on the clock.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api import GitCommandError, GitRepository, StagingError

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_project.api.testing import RepositoryFactory

_PATHS = ("a.txt", "b.txt", "dir/c.txt", "dir/sub/d.txt", "keep.log", "build/out.bin", "other.md")
_TEXTS = ("one\n", "two\n", "three\n", "four\n")
_BRANCHES = ("main", "side", "feature/x")
_SPEC_SETS: tuple[tuple[str, ...], ...] = (
    (".",),
    ("dir",),
    ("a.txt",),
    ("dir/sub",),
    (":(literal)a.txt",),
    (":(glob)**/*.txt",),
    (":(glob)dir/*.txt",),
    ("*.txt",),
    (".", ":(exclude)dir"),
    (".", ":(exclude)*.log"),
    (".", ":(exclude)dir/**"),
    ("a.txt", "b.txt"),
    ("missing.txt",),
    ("build/out.bin",),
    ("keep.log",),
    (":(glob)**/sub/**", ":(exclude)dir/sub/d.txt"),
)
_OBSERVED_SPECS = (_SPEC_SETS[0], _SPEC_SETS[1], _SPEC_SETS[5], _SPEC_SETS[8])
_EXCLUDE_SETS: tuple[tuple[str, ...], ...] = (
    (),
    (":(exclude)dir", ":(exclude)dir/**"),
    (":(exclude)a.txt",),
    (":(exclude)build",),
)
_PATTERN_SETS: tuple[tuple[str, ...], ...] = (
    ("*.log",),
    ("build/",),
    ("/a.txt",),
    ("dir/sub/",),
    ("*.log", "!keep.log"),
    ("other.*",),
)
_PROTECT = ("protected/", "dir/", "dir/sub/", "build/")


@dataclass(frozen=True)
class _Op:
    kind: Literal[
        "write",
        "write_exec",
        "delete",
        "file_over_dir",
        "gitignore",
        "stage",
        "stage_force",
        "unstage",
        "commit",
        "commit_only",
        "commit_empty",
        "reset_index",
        "restore",
        "clean",
        "clean_all",
        "switch",
        "switch_new",
        "excludes",
    ]
    path: str = _PATHS[0]
    text: str = _TEXTS[0]
    specs: int = 0
    ordinal: int = 0
    branch: str = _BRANCHES[0]


_OPS = st.builds(
    _Op,
    kind=st.sampled_from(
        [
            "write",
            "write",
            "write",
            "write_exec",
            "delete",
            "file_over_dir",
            "gitignore",
            "stage",
            "stage",
            "stage_force",
            "unstage",
            "commit",
            "commit",
            "commit_only",
            "commit_empty",
            "reset_index",
            "restore",
            "clean",
            "clean_all",
            "switch",
            "switch_new",
            "excludes",
        ]
    ),
    path=st.sampled_from(_PATHS),
    text=st.sampled_from(_TEXTS),
    specs=st.integers(min_value=0, max_value=len(_SPEC_SETS) - 1),
    ordinal=st.integers(min_value=0, max_value=3),
    branch=st.sampled_from(_BRANCHES),
)


def _write(root: Path, op: _Op, *, executable: bool) -> None:
    target = root / op.path
    if target.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(op.text, encoding="utf-8")
    target.chmod(0o755 if executable else 0o644)


def _file_over_dir(root: Path, op: _Op) -> None:
    """Replace a path's file with a directory, or its directory with a file."""
    target = root / op.path
    if target.is_file():
        target.unlink()
        target.mkdir()
        (target / "inner.txt").write_text(op.text, encoding="utf-8")
    elif target.is_dir():
        for child in sorted(target.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        target.rmdir()
        target.write_text(op.text, encoding="utf-8")


def _gitignore(root: Path, op: _Op) -> None:
    rules = ("*.txt\n!a.txt\n", "sub/\n", "*.bin\n", "")[op.ordinal]
    (root / "dir").mkdir(exist_ok=True)
    (root / "dir" / ".gitignore").write_text(rules, encoding="utf-8")


def _restore(repo: GitRepository, op: _Op) -> None:
    history = repo.recent_subjects(10)
    repo.restore_worktree(history[op.ordinal % len(history)].sha, _EXCLUDE_SETS[op.ordinal])


_FILE_KINDS = {"write", "write_exec", "delete", "file_over_dir", "gitignore"}


def _apply_to_files(root: Path, op: _Op) -> None:
    try:
        if op.kind in {"write", "write_exec"}:
            _write(root, op, executable=op.kind == "write_exec")
        elif op.kind == "delete":
            (root / op.path).unlink(missing_ok=True)
        elif op.kind == "file_over_dir":
            _file_over_dir(root, op)
        else:
            _gitignore(root, op)
    except OSError:
        return  # the path is blocked by a file or directory; both sides see the same state


def _apply_to(repo: GitRepository, root: Path, op: _Op, step: int) -> None:
    if op.kind in _FILE_KINDS:
        _apply_to_files(root, op)
        return
    specs = list(_SPEC_SETS[op.specs])
    actions: dict[str, Callable[[], object]] = {
        "stage": lambda: repo.stage_all(specs),
        "stage_force": lambda: repo.stage_all(specs, force=True),
        "unstage": lambda: repo.unstage(specs),
        "commit": lambda: repo.commit(f"step {step}"),
        "commit_only": lambda: repo.commit(f"only {step}", only=specs),
        "commit_empty": lambda: repo.commit(f"empty {step}", allow_empty=True),
        "reset_index": repo.reset_index,
        "restore": lambda: _restore(repo, op),
        "clean": lambda: repo.clean_untracked(include_ignored=False, protect=_PROTECT[op.ordinal]),
        "clean_all": lambda: repo.clean_untracked(
            include_ignored=True, protect=_PROTECT[op.ordinal]
        ),
        "switch": lambda: repo.switch_branch(op.branch),
        "switch_new": lambda: repo.switch_branch(op.branch, create=True),
        "excludes": lambda: repo.add_excludes(_PATTERN_SETS[op.ordinal % len(_PATTERN_SETS)]),
    }
    actions[op.kind]()


def _attempt(repo: GitRepository, root: Path, op: _Op, step: int) -> str:
    """Apply ``op``; the kind of refusal, or ``ok``."""
    try:
        _apply_to(repo, root, op, step)
    except StagingError as error:
        return f"staging:{sorted(error.unreadable)}"
    except GitCommandError:
        return "refused"
    return "ok"


def _read(call: Callable[[], object]) -> object:
    try:
        return call()
    except GitCommandError:
        return "refused"


def _files(root: Path) -> dict[str, tuple[bytes, bool]]:
    found: dict[str, tuple[bytes, bool]] = {}
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name != ".git"]
        for name in files:
            path = Path(directory) / name
            found[path.relative_to(root).as_posix()] = (path.read_bytes(), os.access(path, os.X_OK))
    return found


def _observe(repo: GitRepository, root: Path) -> dict[str, object]:
    """Every read an implementation promises to answer, without clock-dependent ids."""
    head = repo.head()
    seen: dict[str, object] = {
        "branch": repo.current_branch(),
        "unborn": head is None,
        "subjects": tuple(entry.subject for entry in repo.recent_subjects(50)) if head else (),
        "blobs": tuple(repo.read_blob("HEAD", path) for path in _PATHS),
        "staged": repo.has_staged_changes(),
        "branches": tuple(repo.branch_exists(branch) for branch in _BRANCHES),
        "tracked": tuple(repo.has_tracked_files(spec) for spec in ("dir", "a.txt", "build")),
        "matches": tuple(
            repo.worktree_matches("HEAD", ["."], include_ignored=ignored)
            for ignored in (False, True)
        ),
        "files": _files(root),
    }
    for specs in _OBSERVED_SPECS:
        spec_list = list(specs)
        seen[f"uncommitted {specs}"] = _read(
            lambda spec_list=spec_list: repo.uncommitted_paths(spec_list)
        )
        seen[f"staged {specs}"] = _read(
            lambda spec_list=spec_list: repo.has_staged_changes(spec_list)
        )
        seen[f"since head {specs}"] = _read(
            lambda spec_list=spec_list: repo.tracked_changes_since_head(spec_list)
        )
    seen["root changes"] = _read(lambda: _since_root(repo))
    return seen


def _since_root(repo: GitRepository) -> tuple[object, ...]:
    root = repo.root_commit("HEAD")
    assert root is not None
    return (
        repo.changed_since(root, ["."]),
        repo.diff_name_status(root, "HEAD"),
        repo.first_commit_adding(["a.txt"]) == root,
    )


def _start(root: Path, make: RepositoryFactory) -> GitRepository:
    root.mkdir()
    repo = make(root)
    repo.initialize(initial_branch="main")
    repo.bind()
    (root / "a.txt").write_text("base\n", encoding="utf-8")
    (root / "dir").mkdir()
    (root / "dir" / "c.txt").write_text("base\n", encoding="utf-8")
    repo.stage_all(["."])
    repo.commit("baseline")
    return repo


# Each step starts Git processes in the CLI implementation, so the count is fixed small.
# ``VIBESYS_GIT_CONTRACT_EXAMPLES`` raises it for a long local run.
@settings(max_examples=int(os.environ.get("VIBESYS_GIT_CONTRACT_EXAMPLES", "12")), deadline=None)
@given(operations=st.lists(_OPS, min_size=1, max_size=12))
def test_wide_sequences_agree_with_the_oracle(
    factory: RepositoryFactory, oracle_factory: RepositoryFactory, operations: list[_Op]
) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        roots = (Path(scratch) / "subject", Path(scratch) / "oracle")
        subject = _start(roots[0], factory)
        oracle = _start(roots[1], oracle_factory)
        trace: list[str] = []
        for step, operation in enumerate(operations):
            trace.append(repr(operation))
            outcome = _attempt(subject, roots[0], operation, step)
            assert outcome == _attempt(oracle, roots[1], operation, step), trace
            seen, expected = _observe(subject, roots[0]), _observe(oracle, roots[1])
            assert seen == expected, (trace, [key for key in seen if seen[key] != expected[key]])
