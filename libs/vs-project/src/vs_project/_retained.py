"""Bounded read-only access to a stopped run's original files, without writing state."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from vs_project.errors import StateDocumentDamagedError


@dataclass(frozen=True, slots=True)
class RetainedFile:
    """One regular retained file, listed with its size and not yet read.

    A caller selects files by path and size before reading any bytes, so a large file it
    does not need is never loaded.
    """

    relative_path: PurePosixPath
    size: int
    path: Path = field(repr=False)

    def read(self, *, max_bytes: int) -> bytes:
        """Read at most ``max_bytes``; a larger or vanished file is damaged, never truncated."""
        data = read_retained_bytes(self.path, max_bytes=max_bytes)
        if data is None:
            raise StateDocumentDamagedError.invalid(self.path, "retained file disappeared")
        return data


def read_retained_bytes(path: Path, *, max_bytes: int) -> bytes | None:
    """Read a bounded regular retained file without following its final symlink."""
    if max_bytes < 1:
        message = "max_bytes must be positive"
        raise ValueError(message)
    if path.is_symlink():
        raise StateDocumentDamagedError.invalid(path, "regular retained file required")
    if not path.exists():
        return None
    if not path.is_file():
        raise StateDocumentDamagedError.invalid(path, "regular retained file required")
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise StateDocumentDamagedError.invalid(path, "retained document exceeds byte limit")
    return data


def list_retained(root: Path) -> tuple[RetainedFile, ...]:
    """List every regular file below a contained root; symlinks and special files fail."""
    if not root.exists():
        return ()
    files: list[RetainedFile] = []
    try:
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            if path.is_symlink():
                raise StateDocumentDamagedError.invalid(path, "retained symlink")
            if path.is_dir():
                continue
            if not path.is_file():
                raise StateDocumentDamagedError.invalid(path, "regular retained file required")
            relative = PurePosixPath(path.relative_to(root).as_posix())
            files.append(RetainedFile(relative, path.lstat().st_size, path))
    except OSError as exc:
        raise StateDocumentDamagedError.invalid(root, f"cannot list retained files: {exc}") from exc
    return tuple(files)
