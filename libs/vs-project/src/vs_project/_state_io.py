"""Serialization and atomic I/O for typed VibeSys project state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ValidationError

from vs_project.errors import ProjectStateError, StateModelNotFoundError


def _serialize_state_model(model: BaseModel) -> bytes:
    """Return canonical JSON bytes for a typed state model."""
    try:
        content = json.dumps(
            model.model_dump(mode="json"),
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectStateError.state_serialization_failed() from exc
    return f"{content}\n".encode()


def _serialize_json_object(value: dict[str, object], *, subject: str) -> bytes:
    """Return canonical JSON bytes for a mapping."""
    try:
        content = json.dumps(value, allow_nan=False, indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ProjectStateError.json_serialization_failed(subject) from exc
    return f"{content}\n".encode()


def _atomic_write_model(path: Path, model: BaseModel) -> None:
    """Serialize and atomically replace a typed state model."""
    _atomic_write_bytes(path, _serialize_state_model(model))


def _atomic_write_bytes(path: Path, contents: bytes) -> None:
    """Atomically replace a file with bytes from the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace a UTF-8 text file from the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, object]:
    """Read a JSON object from a metadata path."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectStateError.metadata_file_missing(path) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectStateError.metadata_read_failed(path, exc) from exc
    if not isinstance(raw, dict):
        raise ProjectStateError.metadata_not_object(path)
    return raw


def _load_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required metadata model."""
    try:
        content = path.read_text(encoding="utf-8")
        return model_type.model_validate_json(content, strict=True)
    except FileNotFoundError as exc:
        raise ProjectStateError.metadata_file_missing(path) from exc
    except (OSError, UnicodeError) as exc:
        raise ProjectStateError.metadata_read_failed(path, exc) from exc
    except ValidationError as exc:
        raise ProjectStateError.invalid_metadata(path, _validation_message(exc)) from exc


def _load_state_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required operational-state model."""
    try:
        content = path.read_text(encoding="utf-8")
        return model_type.model_validate_json(content, strict=True)
    except FileNotFoundError as exc:
        raise StateModelNotFoundError.missing(path) from exc
    except (OSError, UnicodeError) as exc:
        raise ProjectStateError.state_read_failed(path, exc) from exc
    except ValidationError as exc:
        raise ProjectStateError.invalid_state_model(path, _validation_message(exc)) from exc


def _validation_message(error: ValidationError) -> str:
    """Render stable validation details without echoing input values."""
    failures: list[str] = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in detail["loc"]) or "metadata"
        failures.append(f"{location}: {detail['msg']}")
    return "; ".join(failures)
