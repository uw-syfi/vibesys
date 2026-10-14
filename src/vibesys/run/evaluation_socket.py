"""Where a run's in-turn evaluation tool service listens, and how an agent container reaches it.

The service is a unix socket on the host. An agent container reaches it through its
parent directory, bind-mounted at the same path: the directory exists before the
container starts (a missing file mount would be created as a directory), the socket
appears inside it once the service listens, and the tool server is handed the one
path that is valid on both sides.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from vs_sandbox.api import EnvironmentBindMount

_SOCKET_NAME = "evaluation.sock"


def evaluation_socket_directory(project_root: Path, run_id: str) -> Path:
    """The run-private directory that holds the evaluation socket."""
    suffix = hashlib.sha256(f"{project_root}:{run_id}".encode()).hexdigest()
    return Path(tempfile.gettempdir()) / f"vse-{suffix[:16]}"


def evaluation_socket_path(project_root: Path, run_id: str) -> Path:
    """The unix socket of one run's in-turn evaluation tool."""
    return evaluation_socket_directory(project_root, run_id) / _SOCKET_NAME


def evaluation_socket_mount(project_root: Path, run_id: str) -> EnvironmentBindMount:
    """Create the socket directory and describe the mount that exposes it to a container."""
    directory = evaluation_socket_directory(project_root, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return EnvironmentBindMount(directory, str(directory), read_only=False)


__all__ = ["evaluation_socket_directory", "evaluation_socket_mount", "evaluation_socket_path"]
