"""The narrow model-storage port used by evaluation scope and operation handlers."""

from pathlib import PurePosixPath
from typing import Protocol

from pydantic import BaseModel


class EvaluationStateNamespace(Protocol):
    """Atomically replace models; reads validate strictly and return detached values.

    Paths are safe, nonempty relative paths. Optional reads return None only
    for absence. Malformed data remains an error. Project's StateNamespace is
    the durable implementation; the in-memory implementation preserves this
    contract for deterministic recovery tests.
    """

    def load[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT:
        """Load a required model or raise the documented missing-model error."""
        ...

    def load_optional[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT | None:
        """Load a model, returning None only for absence."""
        ...

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Atomically replace one model at a safe relative path."""
        ...

    def delete(self, relative_path: str | PurePosixPath) -> bool:
        """Delete an existing model, returning whether it was present."""
        ...


__all__ = ["EvaluationStateNamespace"]
