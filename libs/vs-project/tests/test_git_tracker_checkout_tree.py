"""``GitTracker.checkout_tree`` materializes exactly the requested tree."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from tests.support import run_test_command

from vs_project.api import GitTracker, NullGitTrackerEvents

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
_PATHS = ("a.txt", "b.txt", "c.txt", "d/x.txt", "d/e/y.txt", "f/g/h/z.txt")
_trees = st.dictionaries(st.sampled_from(_PATHS), st.sampled_from(("1", "2", "3")))


def _git(root: Path, *args: str) -> str:
    result = run_test_command(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    )
    return result.stdout.strip()


def _write_tree(root: Path, tree: dict[str, str]) -> None:
    for child in root.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name, text in tree.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _commit(root: Path, tree: dict[str, str]) -> str:
    _write_tree(root, tree)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "tree")
    return _git(root, "rev-parse", "HEAD")


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(head=_trees, source=_trees, clean=st.booleans())
def test_checkout_tree_reproduces_source_tree_over_any_head(
    head: dict[str, str], source: dict[str, str], *, clean: bool
) -> None:
    """Added, deleted, modified, renamed and nested paths all end up as in the source."""
    assume(head or source)  # git cannot restore from an empty tree into an empty index
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _git(root, "init", "-q", "-b", "main")
        source_commit = _commit(root, source)
        _commit(root, head)
        tracker = GitTracker(root, run_id="checkout-tree", events=NullGitTrackerEvents())

        assert tracker.checkout_tree(source_commit, clean=clean)

        assert _files(root) == source
        assert _git(root, "diff", "--cached", "--name-only") == ""


def test_clean_checkout_keeps_nested_paths_absent_from_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    source = _commit(tmp_path, {"a.txt": "1", "new/deep/file.txt": "2"})
    _commit(tmp_path, {"a.txt": "1"})
    tracker = GitTracker(tmp_path, run_id="checkout-tree", events=NullGitTrackerEvents())

    assert tracker.checkout_tree(source, clean=True)

    assert (tmp_path / "new/deep/file.txt").read_text(encoding="utf-8") == "2"


def test_checkout_tree_removes_ignored_files_only_when_asked(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    source = _commit(tmp_path, {".gitignore": "*.out\n", "a.txt": "1"})
    tracker = GitTracker(tmp_path, run_id="checkout-tree", events=NullGitTrackerEvents())
    (tmp_path / "stale.out").write_text("x", encoding="utf-8")

    assert tracker.checkout_tree(source, clean=True)
    assert (tmp_path / "stale.out").exists()
    assert tracker.checkout_tree(source, clean=True, clean_ignored=True)
    assert not (tmp_path / "stale.out").exists()


@settings(max_examples=25, deadline=None)
@given(head=_trees, source=_trees)
def test_matches_tree_agrees_with_an_exact_checkout(
    head: dict[str, str], source: dict[str, str]
) -> None:
    assume(head or source)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _git(root, "init", "-q", "-b", "main")
        (root / ".gitignore").write_text("*.out\n", encoding="utf-8")
        source_commit = _commit(root, {**source, ".gitignore": "*.out\n"})
        _commit(root, {**head, ".gitignore": "*.out\n"})
        tracker = GitTracker(root, run_id="checkout-tree", events=NullGitTrackerEvents())
        (root / "stale.out").write_text("x", encoding="utf-8")

        assert tracker.checkout_tree(source_commit, clean=True, clean_ignored=True)
        assert tracker.matches_tree(source_commit, include_ignored=True)

        (root / "stale.out").write_text("x", encoding="utf-8")
        assert tracker.matches_tree(source_commit)
        assert not tracker.matches_tree(source_commit, include_ignored=True)
        assert tracker.matches_tree(
            source_commit, exempt_paths=("stale.out",), include_ignored=True
        )
        (root / "stale.out").unlink()
        (root / "extra.txt").write_text("x", encoding="utf-8")
        assert not tracker.matches_tree(source_commit)
        (root / "extra.txt").unlink()
        (root / ".gitignore").write_text("*.out\nmore\n", encoding="utf-8")
        assert not tracker.matches_tree(source_commit)
