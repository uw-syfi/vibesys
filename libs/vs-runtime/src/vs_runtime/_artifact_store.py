"""Immutable content-addressed files with durable proof-of-write receipts.

Only ``ArtifactStore.write`` mints receipts. Reads verify the recorded SHA-256;
missing, replaced, or corrupt files never yield unverified contents.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_project.api import StateNamespace


class ArtifactStoreError(Exception):
    """An artifact operation failed, with its offending path identified."""

    def __init__(self, path: Path, reason: str) -> None:
        """Describe the failed operation without hiding its path."""
        self.path = path
        super().__init__(f"Artifact {path}: {reason}")


class ArtifactCorruptionError(ArtifactStoreError):
    """A stored artifact does not match its receipt or content address."""


@dataclass(frozen=True, slots=True, init=False)
class ArtifactReceipt:
    """Proof of durable contents, minted only by ``ArtifactStore.write``.

    ``path`` is absolute. ``size`` counts bytes and ``sha256`` names the file.
    The receipt is an in-process capability, not a deserializable contract.
    """

    path: Path
    size: int
    sha256: str

    def __init__(self) -> None:
        """Reject construction without a successful store write."""
        message = "ArtifactReceipt is produced only by ArtifactStore.write"
        raise TypeError(message)


class ArtifactStore:
    """Store bytes below a Project-owned namespace without rewriting objects.

    Cooperative writers serialize publication with a directory lock. A write
    fsyncs both contents and directory before returning; retries verify and
    reuse an existing object. Corruption is reported rather than repaired.
    """

    def __init__(self, namespace: StateNamespace) -> None:
        """Resolve and durably link the Project-owned artifact directory.

        Raises ``ArtifactStoreError`` if its directory chain cannot be fsynced.
        """
        self._root = namespace.external_directory("artifacts")
        # Project may have just created the namespace and several ancestors.
        # Sync every parent link so a durable receipt cannot outlive its path.
        for directory in (self._root, *self._root.parents):
            try:
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                raise ArtifactStoreError(directory, str(exc)) from exc

    def write(self, content: bytes) -> ArtifactReceipt:
        """Persist bytes atomically and return their immutable receipt.

        Raises ``ArtifactStoreError`` on I/O failure and
        ``ArtifactCorruptionError`` if the content-addressed object is corrupt.
        """
        if not isinstance(content, bytes):
            message = "artifact content must be bytes"
            raise TypeError(message)
        digest = hashlib.sha256(content).hexdigest()
        path = self._root / digest
        try:
            directory_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX)
                self._publish(path, content, directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ArtifactStoreError(path, str(exc)) from exc
        receipt = object.__new__(ArtifactReceipt)
        object.__setattr__(receipt, "path", path)
        object.__setattr__(receipt, "size", len(content))
        object.__setattr__(receipt, "sha256", digest)
        return receipt

    def read(self, receipt: ArtifactReceipt) -> bytes:
        """Read a receipt's bytes after verifying its path, size, and SHA-256."""
        if receipt.path != self._root / receipt.sha256:
            raise ArtifactStoreError(receipt.path, "receipt belongs to another store")
        return self._verified_read(receipt.path, receipt.sha256, receipt.size)

    def _publish(self, path: Path, content: bytes, directory_fd: int) -> None:
        if path.exists() or path.is_symlink():
            self._verified_read(path, path.name, len(content))
            os.fsync(directory_fd)
            return
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self._root, prefix=".pending-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.rename(path)
            os.fsync(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _verified_read(path: Path, digest: str, size: int) -> bytes:
        if path.is_symlink():
            raise ArtifactCorruptionError(path, "content-addressed file is a symlink")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ArtifactStoreError(path, str(exc)) from exc
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise ArtifactCorruptionError(path, "size or SHA-256 does not match")
        return content


__all__ = ["ArtifactCorruptionError", "ArtifactReceipt", "ArtifactStore", "ArtifactStoreError"]
