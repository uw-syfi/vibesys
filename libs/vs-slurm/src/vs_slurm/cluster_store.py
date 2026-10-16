"""Locked, atomically replaced cluster intent records."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path


class OperationStore:
    """Serialize writers and durably replace operation intents."""

    def __init__(self, root: Path) -> None:
        """Create the implementation with its owned operation storage."""
        self._root = root
        root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize access across instances and processes."""
        with (self._root / "operations.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def read(self, identity: str) -> str | None:
        """Read a durable operation record, or return absent."""
        path = self._root / (identity + ".json")
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write(self, identity: str, content: str) -> None:
        """Persist one record and synchronize its containing directory."""
        temporary = self._root / (identity + ".pending")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self._root / (identity + ".json"))
        descriptor = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def records(self) -> list[str]:
        """Read all durable records for job identity lookup."""
        return [path.read_text(encoding="utf-8") for path in sorted(self._root.glob("*.json"))]


class WorkspaceProbeStore:
    """Own one startup probe's durable record, stable payload, and collected marker."""

    def __init__(self, root: Path) -> None:
        """Place all probe-owned local state below ``root``."""
        self._root = root
        self._records = OperationStore(root / "records")

    def lock(self) -> AbstractContextManager[None]:
        """Serialize every prepare/recover/cleanup transition for this run."""
        return self._records.lock()

    def read(self, identity: str) -> str | None:
        """Read a durable lifecycle record."""
        return self._records.read(identity)

    def write(self, identity: str, content: str) -> None:
        """Durably replace a lifecycle record."""
        self._records.write(identity, content)

    def prepare_workspace(self, filename: str, content: str) -> Path:
        """Return the stable one-file workspace, rejecting unexpected retained data."""
        workspace = self._root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        payload = workspace / filename
        if any(path != payload for path in workspace.iterdir()):
            message = "workspace probe store contains an unexpected payload"
            raise ValueError(message)
        payload.write_text(content, encoding="utf-8")
        payload.chmod(0o600)
        os.utime(payload, ns=(0, 0))
        return workspace

    @property
    def artifact_path(self) -> Path:
        """The stable local target used to collect the node marker."""
        return self._root / "node-marker"

    def clear_artifact(self) -> None:
        """Remove any marker left by an interrupted earlier collection."""
        self.artifact_path.unlink(missing_ok=True)

    def read_artifact(self) -> str:
        """Read the collected node marker as UTF-8."""
        return self.artifact_path.read_text(encoding="utf-8")
