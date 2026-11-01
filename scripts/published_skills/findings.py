"""One rule a published skill breaks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule a published skill breaks."""

    path: str
    message: str

    def __str__(self) -> str:
        """The finding as one line, path first."""
        return f"{self.path}: {self.message}"
