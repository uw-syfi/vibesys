"""In-memory implementation of Project's strict subsystem model boundary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vs_project._state import ProjectStateError, _validate_namespace, _validate_state_relative_path
from vs_project._state_io import _serialize_state_model, decode_state_document

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
        key = _validate_state_relative_path(relative_path).as_posix()
        if self._is_directory(key):
            # Production wraps the occupied-by-a-directory read failure the same way.
            error = OSError("is a directory")
            raise ProjectStateError.state_read_failed(Path(key), error)
        data = self.read_bytes(key)
        if data is None:
            return None
        return decode_state_document(model_type, data, source=Path(relative_path))

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Atomically replace detached bytes using the production serializer."""
        self.write_bytes(relative_path, _serialize_state_model(model))

    def read_bytes(self, relative_path: str | PurePosixPath) -> bytes | None:
        """Read subsystem bytes using the production relative-path validator."""
        key = _validate_state_relative_path(relative_path).as_posix()
        if self._is_directory(key):
            raise ProjectStateError.state_path_not_file(Path(key))
        return self._files.get(key)

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        """Atomically replace a safe subsystem file, including deliberate malformed input."""
        key = _validate_state_relative_path(relative_path).as_posix()
        if self._is_directory(key) or any(
            "/".join(parts[:end]) in self._files
            for parts in [key.split("/")]
            for end in range(1, len(parts))
        ):
            message = "a file and a directory cannot share a name"
            raise ProjectStateError.state_file_write_failed(Path(key), OSError(message))
        self._files[key] = contents

    def _is_directory(self, key: str) -> bool:
        return any(name.startswith(f"{key}/") for name in self._files)
