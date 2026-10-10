"""Removal of a test-owned scratch tree that a sandbox just stopped using."""

from __future__ import annotations

import errno
import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PENDING_DELETE_PREFIX = ".nfs"


def remove_scratch_tree(
    path: Path,
    *,
    rmtree: Callable[..., None] = shutil.rmtree,
) -> None:
    """Remove *path*, tolerating deletes the kernel has accepted but not finished.

    A sandbox bind-mounts files from the scratch tree, and the kernel drops
    those mounts a moment after the sandbox process has exited. On NFS,
    removing a file that is still referenced renames it to ``.nfsXXXX`` and the
    server deletes it when the last reference goes. Until then the parent
    directory is not empty and ``rmdir`` fails with ``ENOTEMPTY``, although
    every file the test created is already gone. Such a failure is accepted
    when nothing but those pending-delete entries (and directories) remain
    under the directory, and the rest of the tree is still removed; the kernel
    finishes the removal by itself. Any other leftover, such as a file a
    still-running process wrote, is a real leak and the error is raised.

    *rmtree* is the tree remover, called as ``shutil.rmtree`` is, and exists so
    a test can stage that failure.
    """

    def on_error(_function: Callable[..., object], failed: str, error: BaseException) -> None:
        if not _is_pending_delete(failed, error):
            raise error

    rmtree(path, onexc=on_error)


def _is_pending_delete(failed: str, error: BaseException) -> bool:
    if not isinstance(error, OSError) or error.errno != errno.ENOTEMPTY:
        return False
    return all(
        name.startswith(_PENDING_DELETE_PREFIX)
        for _directory, _subdirectories, files in os.walk(failed)
        for name in files
    )
