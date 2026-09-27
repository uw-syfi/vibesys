"""Confined client for the host-owned Slurm process broker."""

from __future__ import annotations

import json
import socket
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_MAX_FRAME_BYTES = 16 * 1024 * 1024


def run_brokered_process(
    socket_path: Path,
    token: str,
    argv: Sequence[str],
    *,
    stdin: str | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Execute one process through the host broker's constrained capability."""
    request = json.dumps(
        {"token": token, "argv": list(argv), "stdin": stdin, "timeout": timeout},
        separators=(",", ":"),
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(request + b"\n")
        response = b""
        while not response.endswith(b"\n"):
            chunk = client.recv(min(65_536, _MAX_FRAME_BYTES + 1 - len(response)))
            if not chunk:
                break
            response += chunk
            if len(response) > _MAX_FRAME_BYTES:
                message = "Slurm broker response is too large"
                raise RuntimeError(message)
    decoded = json.loads(response)
    if not decoded.get("ok"):
        raise PermissionError(str(decoded.get("error", "Slurm broker rejected request")))
    result = decoded["result"]
    return subprocess.CompletedProcess(
        args=tuple(argv),
        returncode=int(result["returncode"]),
        stdout=str(result["stdout"]),
        stderr=str(result["stderr"]),
    )


__all__ = ["run_brokered_process"]
