"""Tests for removing a scratch tree whose last mounts are still being released."""

from __future__ import annotations

import errno
import os
import shutil
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.scratch_tree import remove_scratch_tree

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_NAMES = st.text(alphabet="abcdefgh0123456789._", min_size=1, max_size=8).filter(
    lambda name: name not in {".", ".."}
)


def _rmtree_stopped_by(leftover_name: str) -> Callable[..., None]:
    """A tree remover that leaves *leftover_name* in ``readonly`` and reports ENOTEMPTY.

    This is the state NFS leaves when a file is unlinked while a bind mount of
    it is still being released. Like ``shutil.rmtree`` with ``onexc``, it goes
    on removing the rest of the tree after the handler returns.
    """

    def rmtree(path: Path, *, onexc: Callable[[object, str, BaseException], object]) -> None:
        readonly = path / "readonly"
        (readonly / "config.json").unlink()
        (readonly / leftover_name).write_text("pending")
        for kept in (readonly, path):
            error = OSError(errno.ENOTEMPTY, "Directory not empty", str(kept))
            if kept == path:
                shutil.rmtree(path / "workspace")
            onexc(os.rmdir, str(kept), error)

    return rmtree


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "scratch"
    (tree / "readonly").mkdir(parents=True)
    (tree / "readonly" / "config.json").write_text("{}")
    (tree / "workspace").mkdir()
    (tree / "workspace" / "written-by-probe.txt").write_text("ok")
    return tree


@given(suffix=_NAMES)
def test_a_pending_delete_entry_does_not_fail_the_removal(
    suffix: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    tree = _tree(tmp_path_factory.mktemp("pending"))

    remove_scratch_tree(tree, rmtree=_rmtree_stopped_by(f".nfs{suffix}"))

    assert not (tree / "workspace").exists()


@given(name=_NAMES.filter(lambda name: not name.startswith(".nfs")))
def test_any_other_leftover_is_reported(
    name: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    tree = _tree(tmp_path_factory.mktemp("leak"))

    with pytest.raises(OSError, match="Directory not empty") as raised:
        remove_scratch_tree(tree, rmtree=_rmtree_stopped_by(name))

    assert raised.value.errno == errno.ENOTEMPTY


def test_other_errors_are_reported_even_when_only_pending_deletes_remain(tmp_path: Path) -> None:
    tree = _tree(tmp_path)

    def denied(path: Path, *, onexc: Callable[[object, str, BaseException], object]) -> None:
        (path / "readonly" / ".nfs1").write_text("pending")
        onexc(os.rmdir, str(path), PermissionError(errno.EACCES, "Permission denied"))

    with pytest.raises(PermissionError):
        remove_scratch_tree(tree, rmtree=denied)


def test_a_tree_is_removed(tmp_path: Path) -> None:
    tree = _tree(tmp_path)

    remove_scratch_tree(tree)

    assert not tree.exists()
