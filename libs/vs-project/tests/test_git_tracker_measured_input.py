"""``GitTracker.measured_input`` names what an evaluation of a commit reads (#2949).

Two commits have one measured input exactly when they agree on every path the evaluation
reads: a change confined to the paths the task declares unread (an agent's notes) keeps the
digest, any other change moves it. The property drives real Git on every Git backend.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.support import run_test_command
from tests.support.real_git import real_git_tracker

from vs_project.api import GIT_BACKEND_ENV, NullGitTrackerEvents
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

_READ = ("main.py", "src/kernel.py")
_UNREAD = ("NOTES.md", "notes/round.md")
_DECLARED = ("NOTES.md", "notes/")
"""The task's declaration: a file and a directory (with the trailing slash a task may write)."""


@pytest.fixture(autouse=True, params=["cli", "pygit2"])
def _backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GIT_BACKEND_ENV, request.param)


@settings(
    max_examples=examples(pr=4, nightly=25),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    edits=st.lists(
        st.tuples(st.sampled_from(_READ + _UNREAD), st.integers(min_value=0, max_value=2)),
        min_size=1,
        max_size=6,
    )
)
@example(
    edits=[
        ("NOTES.md", 1),
        ("notes/round.md", 1),
        ("main.py", 1),
        ("notes/round.md", 2),
        ("NOTES.md", 2),
        ("src/kernel.py", 1),
    ]
)
def test_commits_share_a_measured_input_iff_they_agree_on_every_read_path(
    tmp_path: Path, edits: list[tuple[str, int]]
) -> None:
    root = tmp_path / f"repo-{len(list(tmp_path.iterdir()))}"
    root.mkdir()
    (root / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    tracker = real_git_tracker(root, run_id="input-run", events=NullGitTrackerEvents())
    tracker.init(existing=False)
    contents: dict[str, int] = {}
    seen: list[tuple[str, tuple[tuple[str, int], ...]]] = []
    for number, (path, value) in enumerate(edits):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path} = {value}\n", encoding="utf-8")
        contents[path] = value
        tracker.snapshot(f"step-{number}")
        commit = tracker.current_sha()
        assert commit is not None
        read = tuple(sorted((key, item) for key, item in contents.items() if key in _READ))
        seen.append((tracker.measured_input(commit, _DECLARED), read))
    for digest, read in seen:
        for other_digest, other_read in seen:
            assert (digest == other_digest) == (read == other_read)


_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
_FILES = (*_READ, *_UNREAD, ".vibesys/task.toml")
"""Paths an edit writes: read, unread, and VibeSys's own file, which the digest covers."""
_LINKS = ("link", "notes/alias")
"""Symlinks: one the evaluation reads, one inside an unread directory."""
_TARGETS = ("NOTES.md", "notes/round.md", "main.py", "notes")
_EDITS = st.one_of(
    st.tuples(st.just("write"), st.sampled_from(_FILES), st.integers(min_value=0, max_value=2)),
    st.tuples(st.just("link"), st.sampled_from(_LINKS), st.sampled_from(_TARGETS)),
    st.tuples(st.just("remove"), st.sampled_from(_FILES + _LINKS), st.just(0)),
)


def _git(root: Path, *args: str) -> str:
    return run_test_command(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    ).stdout


def _apply(root: Path, edit: tuple[str, str, int | str]) -> None:
    kind, path, value = edit
    target = root / path
    if kind == "remove":
        if target.is_symlink() or target.is_file():
            target.unlink()
        return
    if target.parent.is_symlink() or target.parent.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        return
    if kind == "write":
        target.write_text(f"{path} = {value}\n", encoding="utf-8")
    else:
        target.symlink_to(os.path.relpath(root / str(value), target.parent))


def _tree(root: Path) -> dict[str, tuple[str, str]]:
    """Every entry below ``root`` but Git's own: a symlink's target, a file's mode and bytes."""
    entries: dict[str, tuple[str, str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        if path.is_symlink():
            entries[relative] = ("link", path.readlink().as_posix())
        elif path.is_file():
            mode = oct(path.stat().st_mode & 0o111)
            entries[relative] = (mode, path.read_text(encoding="utf-8"))
        elif path.is_dir() and not any(path.iterdir()):
            entries[relative] = ("empty", "")
    return entries


@settings(
    max_examples=examples(pr=4, nightly=25),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(edits=st.lists(_EDITS, min_size=1, max_size=6))
@example(
    edits=[
        ("link", "link", "NOTES.md"),
        ("write", "NOTES.md", 1),
        ("write", "notes/round.md", 1),
        ("link", "notes/alias", "main.py"),
        ("write", ".vibesys/task.toml", 1),
        ("write", "NOTES.md", 2),
    ]
)
def test_commits_with_one_measured_input_are_evaluated_on_byte_identical_trees(
    tmp_path: Path, edits: list[tuple[str, str, int | str]]
) -> None:
    """A fresh worktree of a commit, less its unread paths, is exactly what the digest covers.

    Edits write and remove files, and point symlinks into and out of the unread paths. Each
    edit is a commit; for every pair of commits with one digest, the evaluated trees (each
    a fresh linked worktree after ``withhold_unread``) hold the same entries, bytes and modes.
    """
    case = tmp_path / f"case-{len(list(tmp_path.iterdir()))}"
    root = case / "repo"
    root.mkdir(parents=True)
    (root / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    (root / "NOTES.md").write_text("baseline notes\n", encoding="utf-8")
    tracker = real_git_tracker(root, run_id="input-run", events=NullGitTrackerEvents())
    tracker.init(existing=False)
    seen: list[tuple[str, dict[str, tuple[str, str]]]] = []
    for number, edit in enumerate(edits):
        _apply(root, edit)
        _git(root, "add", "-A", "-f", "--", ".", ":(exclude).vibesys/state")
        _git(root, "commit", "-q", "--allow-empty", "-m", f"step {number}")
        commit = _git(root, "rev-parse", "HEAD").strip()
        worktree = case / f"evaluated-{number}"
        _git(root, "worktree", "add", "-q", "--detach", str(worktree), commit)
        real_git_tracker(
            worktree, run_id="input-run", events=NullGitTrackerEvents()
        ).withhold_unread(_DECLARED)
        seen.append((tracker.measured_input(commit, _DECLARED), _tree(worktree)))
    for digest, tree in seen:
        for other_digest, other_tree in seen:
            if digest == other_digest:
                assert tree == other_tree


@pytest.mark.parametrize(
    "declared",
    [(), ("NOTES.md",), ("notes/",), ("NOTES.md", "notes/", "other.md")],
)
def test_another_unread_declaration_is_another_measured_input(
    tmp_path: Path, declared: tuple[str, ...]
) -> None:
    """Even where it withholds nothing more, a changed declaration moves the digest.

    The same commit read under two declarations is evaluated on two trees whenever the
    declarations withhold different paths, so the declaration is part of the input; its
    order and a directory's trailing slash are not.
    """
    (tmp_path / "main.py").write_text("VALUE = 0\n", encoding="utf-8")
    tracker = real_git_tracker(tmp_path, run_id="input-run", events=NullGitTrackerEvents())
    tracker.init(existing=False)
    tracker.snapshot("step")
    commit = tracker.current_sha()
    assert commit is not None
    assert tracker.measured_input(commit, declared) != tracker.measured_input(commit, _DECLARED)
    assert tracker.measured_input(commit, ("notes", "NOTES.md")) == tracker.measured_input(
        commit, _DECLARED
    )
