"""In-memory implementation of Project's strict subsystem model boundary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vs_project._state import ProjectStateError, _validate_namespace, _validate_state_relative_path
from vs_project._state_io import _serialize_state_model, _validation_message

if TYPE_CHECKING:
    from pathlib import PurePosixPath

    from pydantic import BaseModel


def validate_state_namespace(name: str) -> str:
    """Validate a subsystem name through Project's authoritative layout parser."""
    return _validate_namespace(name)


class FakeStateModels:
    """Faithful detached model persistence with the same path and schema validation."""

    def __init__(self) -> None:
        """Create an absent in-memory namespace."""
        self._files: dict[str, bytes] = {}

    def load_optional[ModelT: BaseModel](
        self, relative_path: str | PurePosixPath, model_type: type[ModelT]
    ) -> ModelT | None:
        """Read and validate bytes; absence alone returns None."""
        data = self.read_bytes(relative_path)
        if data is None:
            return None
        try:
            return model_type.model_validate_json(data, strict=True)
        except ValidationError as exc:
            raise ProjectStateError.invalid_state_model(
                Path(relative_path), _validation_message(exc)
            ) from exc

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Atomically replace detached bytes using the production serializer."""
        self.write_bytes(relative_path, _serialize_state_model(model))

    def read_bytes(self, relative_path: str | PurePosixPath) -> bytes | None:
        """Read subsystem bytes using the production relative-path validator."""
        return self._files.get(_validate_state_relative_path(relative_path).as_posix())

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        """Atomically replace a safe subsystem file, including deliberate malformed input."""
        self._files[_validate_state_relative_path(relative_path).as_posix()] = contents
