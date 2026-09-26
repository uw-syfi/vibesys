"""Progress-log contract and filesystem implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

_HEADER = "# Experiment Progress\n\n"


class ProgressLog(Protocol):
    """Read the accumulated run notes and append one rendered entry."""

    def read(self) -> str:
        """Return the accumulated progress text."""
        ...

    def append(self, entry: str) -> None:
        """Append one fully formatted progress entry."""
        ...


class FileProgressLog:
    """Markdown progress log stored at one local path."""

    def __init__(self, path: Path) -> None:
        """Open a local progress log, creating its parent and header if absent."""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text(_HEADER, encoding="utf-8")

    def read(self) -> str:
        """Return the complete log as UTF-8 text."""
        return self._path.read_text(encoding="utf-8")

    def append(self, entry: str) -> None:
        """Append one Markdown entry without changing prior entries."""
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(entry)
