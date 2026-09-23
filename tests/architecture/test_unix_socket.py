"""The Unix socket listening-path length guard."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING

import pytest

from vs_project.api import (
    MAX_SOCKET_PATH_BYTES,
    SocketPathTooLongError,
    validate_socket_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def _too_long(root: Path) -> Path:
    return root / ("d" * MAX_SOCKET_PATH_BYTES) / "server.sock"


def test_a_bindable_path_passes_through_unchanged(socket_dir: Path) -> None:
    path = socket_dir / "server.sock"

    assert validate_socket_path(path) is path


def test_the_limit_matches_what_the_kernel_actually_accepts(socket_dir: Path) -> None:
    """Pin the constant to real ``bind`` behavior rather than to a copied number."""
    name = "a" * (MAX_SOCKET_PATH_BYTES - len(str(socket_dir)) - 1)
    longest = socket_dir / name

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as accepted:
        accepted.bind(str(longest))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rejected:  # noqa: SIM117
        with pytest.raises(OSError, match="too long"):
            rejected.bind(f"{longest}a")


def test_an_overlong_path_is_rejected_by_name_and_size(tmp_path: Path) -> None:
    path = _too_long(tmp_path)

    with pytest.raises(SocketPathTooLongError) as failure:
        validate_socket_path(path)

    message = str(failure.value)
    assert str(path) in message
    assert str(MAX_SOCKET_PATH_BYTES) in message
    assert failure.value.path == path
