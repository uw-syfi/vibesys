"""Locked, atomically replaced cluster intent records."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
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
