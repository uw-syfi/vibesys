"""Serialization and atomic I/O for typed VibeSys project state."""

# TRY003: these boundary errors deliberately embed the offending metadata path
# and value.
# ruff: noqa: TRY003

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ValidationError

from vs_loop_state import RoundRecord, serialize_round_record
from vs_project.errors import ProjectStateError, StateModelNotFoundError


def _validate_portable_round(record: RoundRecord, *, source: Path | None = None) -> None:
    """Reject machine-local paths and non-finite metrics at the commit boundary."""
    subject = f"Completed-round metadata at {source}" if source is not None else "Completed-round"
    for field_name in ("evaluation_artifact", "candidate_evaluation_artifact"):
        value = getattr(record, field_name)
        if value is None:
            continue
        artifact = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or artifact.is_absolute()
            or artifact == PurePosixPath(".")
            or ".." in artifact.parts
        ):
            raise ProjectStateError(
                f"{subject} {field_name} must be a portable project-relative path"
            )
    metric_values = [
        record.perf_metric,
        *record.metrics.values(),
        *record.candidate_metrics.values(),
    ]
    if any(value is not None and not math.isfinite(value) for value in metric_values):
        raise ProjectStateError(f"{subject} metrics must be finite numbers")


def serialize_round(record: RoundRecord) -> bytes:
    """Return validated canonical bytes for one portable completed round."""
    if record.round_number < 1:
        raise ProjectStateError(f"Round number must be positive, got {record.round_number}")
    _validate_portable_round(record)
    try:
        contents = json.dumps(
            serialize_round_record(record),
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectStateError(
            f"Could not serialize completed-round metadata for round {record.round_number}"
        ) from exc
    return f"{contents}\n".encode()


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
        raise ProjectStateError("Could not serialize VibeSys state model") from exc
    return f"{content}\n".encode()


def _serialize_json_object(value: dict[str, object], *, subject: str) -> bytes:
    """Return canonical JSON bytes for a mapping."""
    try:
        content = json.dumps(value, allow_nan=False, indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ProjectStateError(f"Could not serialize {subject}") from exc
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
        raise ProjectStateError(f"VibeSys metadata file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectStateError(f"Could not read VibeSys metadata at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProjectStateError(f"Expected a JSON object in VibeSys metadata at {path}")
    return raw


def _load_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required metadata model."""
    try:
        content = path.read_text(encoding="utf-8")
        return model_type.model_validate_json(content, strict=True)
    except FileNotFoundError as exc:
        raise ProjectStateError(f"VibeSys metadata file does not exist: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise ProjectStateError(f"Could not read VibeSys metadata at {path}: {exc}") from exc
    except ValidationError as exc:
        raise ProjectStateError(
            f"Invalid VibeSys metadata at {path}: {_validation_message(exc)}"
        ) from exc


def _load_state_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required operational-state model."""
    try:
        content = path.read_text(encoding="utf-8")
        return model_type.model_validate_json(content, strict=True)
    except FileNotFoundError as exc:
        raise StateModelNotFoundError(f"VibeSys state model does not exist: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise ProjectStateError(f"Could not read VibeSys state model at {path}: {exc}") from exc
    except ValidationError as exc:
        raise ProjectStateError(
            f"Invalid VibeSys state model at {path}: {_validation_message(exc)}"
        ) from exc


def _validation_message(error: ValidationError) -> str:
    """Render stable validation details without echoing input values."""
    failures: list[str] = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in detail["loc"]) or "metadata"
        failures.append(f"{location}: {detail['msg']}")
    return "; ".join(failures)
