"""Reads and writes through a ``WorkspaceTree`` stay inside its root for every path shape."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import (
    EntryKind,
    NotRegularFileError,
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
    outside = root / "outside"
    return {
        path.relative_to(outside).as_posix(): path.read_bytes()
        for path in outside.rglob("*")
        if path.is_file()
    }


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
