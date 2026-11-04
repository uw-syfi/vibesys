"""Stable input fingerprints, excluding generated state and local configuration."""

import stat
from pathlib import Path
from typing import Protocol

from vs_project._paths import CONFIGURATION_DIRECTORY_NAME, STATE_DIRECTORY_PARTS
from vs_project.errors import ProjectStateError

_EXCLUDED_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "agent.toml",
        "node_modules",
    }
)


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object:
        """Add bytes to the digest state."""


def _is_excluded(relative: Path) -> bool:
    if (
        relative.parts == (CONFIGURATION_DIRECTORY_NAME,)
        or relative.parts[:2] == STATE_DIRECTORY_PARTS
    ):
        return True
    for part in relative.parts:
        if part in _EXCLUDED_NAMES or part == ".env" or part.startswith(".env."):
            return True
        if part.endswith((".pyc", ".pyo")):
            return True
    return False


def _update_fingerprint(digest: _Digest, path: Path, relative: Path) -> None:
    update = digest.update
    encoded_path = relative.as_posix().encode("utf-8", "surrogateescape")
    try:
        metadata = path.lstat()
        mode = metadata.st_mode
        if stat.S_ISDIR(mode):
            update(b"D\0" + encoded_path + b"\0")
        elif stat.S_ISLNK(mode):
            target = str(path.readlink()).encode("utf-8", "surrogateescape")
            update(b"L\0" + encoded_path + b"\0" + target + b"\0")
        elif stat.S_ISREG(mode):
            executable = b"1" if mode & 0o111 else b"0"
            update(b"F\0" + encoded_path + b"\0" + executable + b"\0")
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    update(block)
            update(b"\0")
        else:
            message = f"Unsupported input file type: {path}"
            raise ProjectStateError(message)
    except OSError as exc:
        message = f"Could not fingerprint project input {path}: {exc}"
        raise ProjectStateError(message) from exc
