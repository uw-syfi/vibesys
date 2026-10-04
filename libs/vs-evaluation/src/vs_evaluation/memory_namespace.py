"""Faithful in-memory model storage for evaluation-handler recovery tests."""

import json
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ValidationError

from vs_project.api import ProjectStateError, StateModelNotFoundError


class InMemoryEvaluationNamespace:
    """Store serialized models, preserving strict reads and replacement atomicity."""

    def __init__(self) -> None:
        """Start with no external operational state."""
        self._values: dict[str, str] = {}

    def load[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT:
        """Return a detached, strictly validated required model."""
        key = self._key(relative_path)
        if key not in self._values:
            raise StateModelNotFoundError.missing(Path(key))
        try:
            return model_type.model_validate_json(self._values[key], strict=True)
        except ValidationError as error:
            failures = [
                f"{'.'.join(str(part) for part in detail['loc']) or 'metadata'}: {detail['msg']}"
                for detail in error.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
            raise ProjectStateError.invalid_state_model(Path(key), "; ".join(failures)) from error

    def load_optional[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT | None:
        """Return None only when the model is absent."""
        if self._key(relative_path) not in self._values:
            return None
        return self.load(relative_path, model_type)

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Atomically replace the serialized model without retaining caller aliases."""
        key = self._key(relative_path)
        try:
            serialized = json.dumps(model.model_dump(mode="json", round_trip=True), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ProjectStateError.state_serialization_failed() from error
        self._values[key] = serialized

    def delete(self, relative_path: str | PurePosixPath) -> bool:
        """Delete an existing model, reporting whether it was present."""
        return self._values.pop(self._key(relative_path), None) is not None

    @staticmethod
    def _key(relative_path: str | PurePosixPath) -> str:
        path = PurePosixPath(relative_path)
        if (
            not str(relative_path)
            or (
                isinstance(relative_path, str)
                and any(not part for part in relative_path.split("/"))
            )
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or "\\" in str(relative_path)
        ):
            raise ProjectStateError.invalid_state_file_path(relative_path, portable=True)
        return path.as_posix()


__all__ = ["InMemoryEvaluationNamespace"]
