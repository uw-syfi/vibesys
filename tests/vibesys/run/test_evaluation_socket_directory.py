"""The evaluation socket's directory is the run user's own, private directory.

It lives in the shared temporary directory under a name other local users can
compute, so one of them can create the path first. Whoever controls the
directory can replace the socket and answer the agent's evaluation calls.
"""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from vibesys.run.evaluation_socket import (
    EvaluationSocketDirectoryError,
    evaluation_socket_directory,
    evaluation_socket_mount,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def run_id(tmp_path: Path) -> Iterator[str]:
    """A run id no other test uses, and removal of whatever its directory became."""
    run = f"20261014-000000-{uuid.uuid4().hex[:8]}-socket"
    yield run
    directory = evaluation_socket_directory(tmp_path, run)
    if directory.is_symlink():
        directory.unlink()
    with suppress(FileNotFoundError):
        directory.chmod(0o700)
        directory.rmdir()


def test_a_new_directory_is_private(tmp_path: Path, run_id: str) -> None:
    mount = evaluation_socket_mount(tmp_path, run_id)

    status = mount.host_path.lstat()
    assert stat.S_ISDIR(status.st_mode)
    assert status.st_uid == os.geteuid()
    assert stat.S_IMODE(status.st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o750, 0o500])
def test_the_users_own_directory_is_reused_and_made_private(
    tmp_path: Path, run_id: str, mode: int
) -> None:
    """A resumed or crashed run of the same id left its directory behind."""
    directory = evaluation_socket_directory(tmp_path, run_id)
    directory.mkdir()
    directory.chmod(mode)

    mount = evaluation_socket_mount(tmp_path, run_id)

    assert mount.host_path == directory
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o777, 0o1777, 0o720, 0o702])
def test_the_users_own_directory_that_others_can_write_is_refused(
    tmp_path: Path, run_id: str, mode: int
) -> None:
    """Another user may already have put something in it; ``chmod`` cannot undo that."""
    directory = evaluation_socket_directory(tmp_path, run_id)
    directory.mkdir()
    directory.chmod(mode)

    with pytest.raises(EvaluationSocketDirectoryError, match=str(directory)):
        evaluation_socket_mount(tmp_path, run_id)
    assert stat.S_IMODE(directory.lstat().st_mode) == mode


def test_a_symlink_planted_at_the_path_is_refused(tmp_path: Path, run_id: str) -> None:
    """A link to a directory someone else controls would put the socket there."""
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir(mode=0o777)
    directory = evaluation_socket_directory(tmp_path, run_id)
    directory.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(EvaluationSocketDirectoryError, match=str(directory)):
        evaluation_socket_mount(tmp_path, run_id)
    assert directory.is_symlink()


def test_a_file_planted_at_the_path_is_refused(tmp_path: Path, run_id: str) -> None:
    directory = evaluation_socket_directory(tmp_path, run_id)
    directory.touch()
    try:
        with pytest.raises((EvaluationSocketDirectoryError, FileExistsError)):
            evaluation_socket_mount(tmp_path, run_id)
    finally:
        directory.unlink()


def test_another_users_directory_is_refused_and_left_alone(tmp_path: Path, run_id: str) -> None:
    """Owned by someone else: neither used nor touched, even when only it can write."""
    directory = evaluation_socket_directory(tmp_path, run_id)
    directory.mkdir()
    directory.chmod(0o755)

    with pytest.raises(EvaluationSocketDirectoryError, match=str(directory)):
        evaluation_socket_mount(tmp_path, run_id, owner=os.geteuid() + 1)
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o755
