"""Reads and writes through a ``WorkspaceTree`` stay inside its root for every path shape."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_project.api.workspace_tree import (
    EntryKind,
    NotRegularFileError,
    TreeLimitError,
    WorkspacePathError,
    open_workspace_tree,
)
from vs_sim.api.testing import examples

_OUTSIDE_BYTES = b"outside the workspace\n"
_NAMES = ("a", "b", "c")
_KINDS = ("dir", "file", "fifo", "link_out_dir", "link_out_file", "link_in_dir", "link_in_file")

_tree_specs = st.lists(
    st.tuples(st.lists(st.sampled_from(_NAMES), min_size=1, max_size=3), st.sampled_from(_KINDS)),
    max_size=10,
)
_queries = st.tuples(
    st.booleans(),
    st.lists(st.sampled_from((*_NAMES, ".", "..")), min_size=1, max_size=4),
)


def _build(root: Path, spec: list[tuple[list[str], str]]) -> Path:
    """Build the workspace ``spec`` describes below ``root``; return the workspace."""
    outside = root / "outside"
    outside.mkdir()
    (outside / "a").write_bytes(_OUTSIDE_BYTES)
    (outside / "b").mkdir()
    (outside / "b" / "a").write_bytes(_OUTSIDE_BYTES)
    workspace = root / "workspace"
    (workspace / "real-dir").mkdir(parents=True)
    (workspace / "real-dir" / "a").write_bytes(b"inside\n")
    for parts, kind in spec:
        path = workspace.joinpath(*parts)
        if not all(workspace.joinpath(*parts[:depth]).is_dir() for depth in range(1, len(parts))):
            continue
        if os.path.lexists(path):
            continue
        match kind:
            case "dir":
                path.mkdir()
            case "file":
                path.write_bytes("/".join(parts).encode())
            case "fifo":
                os.mkfifo(path)
            case "link_out_dir":
                path.symlink_to(outside, target_is_directory=True)
            case "link_out_file":
                path.symlink_to(outside / "a")
            case "link_in_dir":
                path.symlink_to(workspace / "real-dir", target_is_directory=True)
            case _:
                path.symlink_to(workspace / "real-dir" / "a")
    return workspace


def _query_text(query: tuple[bool, list[str]]) -> str:
    absolute, parts = query
    return ("/" if absolute else "") + "/".join(parts)


def _expected(
    workspace: Path, query: tuple[bool, list[str]], *, last_may_be_symlink: bool = False
) -> Path | None:
    """The file the query names when no component is ``..`` or a symlink, else ``None``.

    A write replaces the last component, so a symlink there is replaced, not followed.
    """
    absolute, parts = query
    if absolute or ".." in parts:
        return None
    names = [part for part in parts if part != "."]
    current = workspace
    for depth, name in enumerate(names, start=1):
        current /= name
        if current.is_symlink() and not (last_may_be_symlink and depth == len(names)):
            return None
    return current


def _outside_snapshot(root: Path) -> dict[str, bytes]:
    """Every file's bytes and every directory (as ``b""``) below ``root/outside``."""
    return {
        key: entry
        for key, entry in _entries(root).items()
        if key == "outside" or key.startswith("outside/")
    }


def _entries(root: Path) -> dict[str, bytes]:
    """Every entry below ``root``, seen without following a symlink.

    A file maps to its bytes, a directory to ``b"/"`` and a symlink to ``b"->"`` and its
    target, so a removal, a write or a replaced link all change the map.
    """
    entries: dict[str, bytes] = {}
    for directory, names, files in os.walk(root):
        for name in (*names, *files):
            path = Path(directory, name)
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                entries[key] = b"->" + os.fsencode(path.readlink())
            elif path.is_dir():
                entries[key] = b"/"
            elif path.is_file():
                entries[key] = path.read_bytes()
    return entries


@settings(max_examples=examples(pr=40, nightly=600), deadline=None)
@given(spec=_tree_specs, query=_queries)
def test_a_read_returns_the_file_the_path_names_without_following_a_symlink(
    spec: list[tuple[list[str], str]], query: tuple[bool, list[str]]
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        workspace = _build(Path(directory), spec)
        expected = _expected(workspace, query)
        with open_workspace_tree(workspace) as tree:
            try:
                data: bytes | None = tree.read_bytes(_query_text(query), max_bytes=1024)
            except WorkspacePathError:
                assert expected is None or expected == workspace
                return
            except OSError:
                data = None
        assert expected is not None
        if data is None:
            assert not (expected.is_file() and not expected.is_symlink())
        else:
            assert data == expected.read_bytes()
            assert data != _OUTSIDE_BYTES


@settings(max_examples=examples(pr=25, nightly=400), deadline=None)
@given(spec=_tree_specs, query=_queries, contents=st.binary(max_size=64))
def test_a_write_lands_where_the_path_names_inside_the_workspace_or_nowhere(
    spec: list[tuple[list[str], str]], query: tuple[bool, list[str]], contents: bytes
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = _build(root, spec)
        before = _outside_snapshot(root)
        expected = _expected(workspace, query, last_may_be_symlink=True)
        try:
            with open_workspace_tree(workspace) as tree:
                tree.write_bytes(_query_text(query), contents)
            written = True
        except (WorkspacePathError, OSError):
            written = False
        assert _outside_snapshot(root) == before
        if written:
            assert expected is not None
            assert not expected.is_symlink()
            assert expected.read_bytes() == contents


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with open_workspace_tree(tmp_path) as tree, pytest.raises(NotRegularFileError):
        tree.read_bytes("pipe", max_bytes=16)
    with open_workspace_tree(tmp_path) as tree:
        assert tree.kind("pipe") is EntryKind.OTHER


def test_files_below_refuses_a_symlink_at_any_depth(tmp_path: Path) -> None:
    (tmp_path / "inputs" / "deep").mkdir(parents=True)
    (tmp_path / "inputs" / "one.py").write_text("1\n")
    (tmp_path / "inputs" / "deep" / "two.py").write_text("2\n")
    with open_workspace_tree(tmp_path) as tree:
        assert tree.files_below("inputs") == ("inputs/deep/two.py", "inputs/one.py")
    (tmp_path / "inputs" / "deep" / "link").symlink_to(tmp_path)
    with open_workspace_tree(tmp_path) as tree, pytest.raises(WorkspacePathError):
        tree.files_below("inputs")


def _without(entries: dict[str, bytes], removed: str) -> dict[str, bytes]:
    return {
        key: value
        for key, value in entries.items()
        if key != removed and not key.startswith(f"{removed}/")
    }


@settings(max_examples=examples(pr=25, nightly=400), deadline=None)
@given(spec=_tree_specs, query=_queries)
def test_a_removal_takes_only_the_entry_the_path_names_never_through_a_symlink(
    spec: list[tuple[list[str], str]], query: tuple[bool, list[str]]
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = _build(root, spec)
        before = _entries(root)
        expected = _expected(workspace, query, last_may_be_symlink=True)
        try:
            with open_workspace_tree(workspace) as tree:
                tree.remove(_query_text(query))
        except WorkspacePathError:
            assert _entries(root) == before
            return
        assert expected is not None
        assert expected != workspace
        assert _entries(root) == _without(before, expected.relative_to(root).as_posix())


def _host_skill(root: Path) -> Path:
    source = root / "source"
    (source / "references" / "deep").mkdir(parents=True)
    (source / "SKILL.md").write_text("# skill\n")
    (source / "references" / "deep" / "note.md").write_text("note\n")
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "run.sh").chmod(0o755)
    (source / "skipped").mkdir()
    (source / "skipped" / "secret").write_text("secret\n")
    (source / "alias.md").symlink_to("SKILL.md")
    return source


@settings(max_examples=examples(pr=10, nightly=400), deadline=None)
@given(spec=_tree_specs, query=_queries)
def test_a_copy_lands_only_at_the_new_directory_the_path_names(
    spec: list[tuple[list[str], str]], query: tuple[bool, list[str]]
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = _build(root, spec)
        source = _host_skill(root / "host")
        before = _entries(root)
        expected = _expected(workspace, query)
        try:
            with open_workspace_tree(workspace) as tree:
                copied = tree.copy_in(
                    source, _query_text(query), ignore=lambda _, names: {"skipped"} & set(names)
                )
        except (WorkspacePathError, OSError):
            assert _entries(root) == before
            return
        assert expected is not None
        prefix = expected.relative_to(root).as_posix()
        assert prefix not in before
        added = {k: v for k, v in _entries(root).items() if k not in before}
        assert added == {
            prefix: b"/",
            f"{prefix}/SKILL.md": b"# skill\n",
            f"{prefix}/run.sh": b"#!/bin/sh\n",
            f"{prefix}/references": b"/",
            f"{prefix}/references/deep": b"/",
            f"{prefix}/references/deep/note.md": b"note\n",
        }
        assert {k: v for k, v in _entries(root).items() if k in before} == before
        assert os.access(expected / "run.sh", os.X_OK)
        assert copied.links == (PurePosixPath("alias.md"),)
        assert PurePosixPath("skipped") not in copied.copied


def test_planted_temporary_names_never_block_a_write(tmp_path: Path) -> None:
    """Regression: temporaries were numbered, so planting the next 64 names blocked a write."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("host\n")
    for number in range(4096):
        (workspace / f".notes.txt.{number}.tmp").symlink_to(outside)

    with open_workspace_tree(workspace) as tree:
        tree.write_bytes("notes.txt", b"agent\n")

    assert (workspace / "notes.txt").read_bytes() == b"agent\n"
    assert outside.read_text() == "host\n"


@settings(max_examples=examples(pr=15, nightly=300), deadline=None)
@given(
    files=st.integers(min_value=0, max_value=6),
    directories=st.integers(min_value=0, max_value=6),
    limit=st.integers(min_value=0, max_value=7),
)
def test_a_limited_listing_is_the_full_listing_or_refused_past_the_limit(
    *, files: int, directories: int, limit: int
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        nested = workspace / "inputs"
        nested.mkdir()
        for number in range(directories):
            nested = nested / f"d{number}"
            nested.mkdir()
        for number in range(files):
            (nested / f"f{number}").write_bytes(b"")

        with open_workspace_tree(workspace) as tree:
            full = tree.files_below("inputs")
            if max(files, directories) > limit:
                with pytest.raises(TreeLimitError):
                    tree.files_below("inputs", limit=limit)
            else:
                assert tree.files_below("inputs", limit=limit) == full


def test_an_overwrite_keeps_the_inode_and_never_writes_through_a_link(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "state").mkdir(parents=True)
    kept = workspace / "state" / "a.json"
    kept.write_bytes(b"old contents\n")
    inode = kept.stat().st_ino
    outside = tmp_path / "outside"
    outside.write_bytes(b"host\n")
    (workspace / "state" / "link.json").symlink_to(outside)

    with open_workspace_tree(workspace) as tree:
        tree.overwrite("state/a.json", b"new\n")
        tree.overwrite("fresh/b.json", b"b\n")
        with pytest.raises(WorkspacePathError):
            tree.overwrite("state/link.json", b"agent\n")

    assert kept.read_bytes() == b"new\n"
    assert kept.stat().st_ino == inode
    assert (workspace / "fresh" / "b.json").read_bytes() == b"b\n"
    assert outside.read_bytes() == b"host\n"


def test_entries_named_finds_every_match_without_entering_a_link_or_a_match(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    (outside / "linked" / ".git").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    (workspace / ".git" / "modules" / "x" / ".git").mkdir(parents=True)
    (workspace / "a" / "b" / ".git").mkdir(parents=True)
    (workspace / "a" / "b" / ".git" / ".git").mkdir()
    (workspace / "c").mkdir()
    (workspace / "c" / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (workspace / "d").symlink_to(outside)
    (workspace / "e").mkdir()
    (workspace / "e" / ".git").symlink_to(outside / "linked")

    with open_workspace_tree(workspace) as tree:
        found = tree.entries_named(".git", limit=100)

    assert found == (".git", "a/b/.git", "c/.git", "e/.git")


@settings(max_examples=examples(pr=4, nightly=50), deadline=None)
@given(depth=st.integers(min_value=0, max_value=6), limit=st.integers(min_value=0, max_value=8))
def test_entries_named_is_refused_past_the_limit_or_complete(*, depth: int, limit: int) -> None:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        nested = workspace
        for number in range(depth):
            nested = nested / f"d{number}"
            nested.mkdir()
        (nested / ".git").mkdir()
        expected = (PurePosixPath(*(f"d{n}" for n in range(depth)), ".git").as_posix(),)

        with open_workspace_tree(workspace) as tree:
            if depth + 1 > limit:
                with pytest.raises(TreeLimitError):
                    tree.entries_named(".git", limit=limit)
            else:
                assert tree.entries_named(".git", limit=limit) == expected
