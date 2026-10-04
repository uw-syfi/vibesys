"""Strict subsystem-owned model persistence within a Project namespace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import PurePosixPath

    from pydantic import BaseModel


class StateModels(Protocol):
    """Read and atomically replace validated subsystem state at safe relative paths.

    Missing optional files return None; malformed data and unsafe paths are
    errors. Persisted models are validated strictly when loaded. Implementations
    must detach stored values from the caller's mutable model.
    """

    def load_optional[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT | None:
        """Read an optional model, preserving validation errors."""
        ...

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Serialize and atomically replace the named model."""
        ...
