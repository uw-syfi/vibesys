"""The one rule for a directory only its owner may use, and where a user's runtime root lives.

A Unix socket, a lock file or a lease lives in a directory on node-local disk that
other users of the host can reach: ``/tmp`` is shared, and a path that others can
compute (a name derived from a project root and a run id, or a fixed
``/tmp/vibesys-<uid>``) can be created by them first. Whoever controls the directory can
replace what is in it, so a directory is used only after it has been checked, and a
planted one is refused instead of repaired.

:func:`user_runtime_root` says where the per-user root is, :func:`ensure_private_directory`
creates and checks one directory. The registry of live servers, the evaluation tool's
socket directory and the Modal evaluator helper (which restates this rule because it is
mounted alone into a container and cannot import it; a test runs both against the same
directories) all go through this rule.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_PRIVATE_MODE = 0o700
"""The mode a private directory is created with and left at."""
_GROUP_OR_OTHER_WRITE = 0o022
_FALLBACK_PARENT = Path("/tmp")  # noqa: S108  # lint-waiver: LW-178202 [S108]; the per-user runtime fallback must be a short, node-local, session-independent path; `tempfile.gettempdir()` follows `$TMPDIR`, which differs between macOS GUI and SSH sessions and can be long enough to break the Unix socket path limit
"""Parent of the fallback root when ``$XDG_RUNTIME_DIR`` is unset."""


class PrivateDirectoryError(PermissionError):
    """A directory that must be one user's own is a symlink, a file, or another user's."""

    @classmethod
    def not_private(cls, path: Path, owner: int) -> PrivateDirectoryError:
        """Name the path and what it must be."""
        return cls(
            f"{path} must be a directory owned by uid {owner} and not writable by group or "
            "other, not a symlink; remove it if nothing of yours is using it"
        )


def user_runtime_root(environ: Mapping[str, str], uid: int) -> Path:
    """The node-local root for one user's sockets, locks and leases; nothing is created.

    ``$XDG_RUNTIME_DIR/vibesys`` when that is an absolute path (systemd makes it per
    user, local and private), else ``/tmp/vibesys-<uid>``. Never under ``$HOME``, which
    may be on NFS where ``flock`` and sockets are unreliable. Both stay short enough
    for a socket path below the Unix limit.
    """
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    if runtime and Path(runtime).is_absolute():
        return Path(runtime) / "vibesys"
    return _FALLBACK_PARENT / f"vibesys-{uid}"


def ensure_private_directory(path: Path, owner: int) -> Path:
    """Create ``path`` mode 0700 if absent, check it is ``owner``'s own, and return it.

    An existing directory of ``owner``'s (a resumed or crashed run's) is reused and
    made 0700. Raises :class:`PrivateDirectoryError` if the path is a symlink, is not a
    directory, belongs to anyone else, or is writable by group or other: ``chmod``
    closes access from now on but cannot remove what another user already put inside.
    Creating it can fail with ``OSError`` (a file in the way, no permission).
    """
    path.mkdir(mode=_PRIVATE_MODE, exist_ok=True)
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != owner
        or status.st_mode & _GROUP_OR_OTHER_WRITE
    ):
        raise PrivateDirectoryError.not_private(path, owner)
    if stat.S_IMODE(status.st_mode) != _PRIVATE_MODE:
        path.chmod(_PRIVATE_MODE)
    return path
