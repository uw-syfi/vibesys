"""Where a run's in-turn evaluation tool service listens, and how an agent container reaches it.

The service is a unix socket on the host. An agent container reaches it through its
parent directory, bind-mounted at the same path: the directory exists before the
container starts (a missing file mount would be created as a directory), the socket
appears inside it once the service listens, and the tool server is handed the one
path that is valid on both sides.

The directory sits in the shared temporary directory under a name derived from the
project root and the run id, both of which other local users can learn (a detached
run's id is on its command line). It is therefore created private, and a path that
another user planted first, as a directory of theirs or as a symlink, is refused
rather than used: whoever controls the directory can replace the socket and answer
the agent's evaluation calls, token included.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from vs_sandbox.api import EnvironmentBindMount

_SOCKET_NAME = "evaluation.sock"
_PRIVATE_DIRECTORY = 0o700


class EvaluationSocketDirectoryError(PermissionError):
    """The evaluation socket's directory exists but is not this user's own directory."""

    @classmethod
    def planted(cls, directory: Path, owner: int) -> EvaluationSocketDirectoryError:
        """Name the path and the owner it must have."""
        return cls(
            f"{directory} must be a directory owned by uid {owner} and writable only by "
            "it, not a symlink; remove it if no run of yours is using it"
        )


def evaluation_socket_directory(project_root: Path, run_id: str) -> Path:
    """The run-private directory that holds the evaluation socket."""
    suffix = hashlib.sha256(f"{project_root}:{run_id}".encode()).hexdigest()
    return Path(tempfile.gettempdir()) / f"vse-{suffix[:16]}"


def evaluation_socket_path(project_root: Path, run_id: str) -> Path:
    """The unix socket of one run's in-turn evaluation tool."""
    return evaluation_socket_directory(project_root, run_id) / _SOCKET_NAME


def evaluation_socket_mount(
    project_root: Path, run_id: str, *, owner: int | None = None
) -> EnvironmentBindMount:
    """Create the socket directory private and describe the mount that exposes it to a container.

    ``owner`` is the uid the directory must belong to, this process's by default.
    An existing directory of ``owner``'s (a resumed or crashed run's) is reused
    and made private. Raises ``EvaluationSocketDirectoryError`` if the path is a
    symlink, is not a directory, belongs to anyone else, or is writable by group
    or other: ``chmod`` closes access from now on but cannot remove what another
    user already put inside (the rule ``vs_sandbox``'s modal evaluator applies to
    its runtime directory).
    """
    owner = os.geteuid() if owner is None else owner
    directory = evaluation_socket_directory(project_root, run_id)
    directory.mkdir(mode=_PRIVATE_DIRECTORY, exist_ok=True)
    status = directory.lstat()
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != owner or status.st_mode & 0o022:
        raise EvaluationSocketDirectoryError.planted(directory, owner)
    if stat.S_IMODE(status.st_mode) != _PRIVATE_DIRECTORY:
        directory.chmod(_PRIVATE_DIRECTORY)
    return EnvironmentBindMount(directory, str(directory), read_only=False)


__all__ = [
    "EvaluationSocketDirectoryError",
    "evaluation_socket_directory",
    "evaluation_socket_mount",
    "evaluation_socket_path",
]
