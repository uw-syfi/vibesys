"""Decode JSON from outside the process into a typed model, or say why it cannot.

This is the one decode path for bytes a store or an edge adapter receives: a file
another process or an agent wrote, a CLI's output, a peer's frame. Each step bounds
what hostile input can cost:

1. The input is capped at ``max_bytes`` (a file is read at most one byte past it).
2. pydantic's JSON parser parses it; it bounds nesting depth, so deep input is a
   validation error, not a ``RecursionError``, and bad UTF-8 is an error too.
3. The parsed value is validated against a strict target: every model, dataclass and
   TypedDict the target decodes, at any depth, must forbid unknown keys.

Every failure is returned as an ``Unreadable`` value naming the source, the kind of
failure and the key path, never echoing the input. Nothing here raises for bad input.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, overload

from pydantic import BaseModel, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class ValidationOptions:
    """How pydantic validates the parsed value."""

    strict: bool = False
    """pydantic's strict mode: no coercion between JSON types."""
    context: Mapping[str, Any] | None = None
    """Validation context handed to the target's validators."""


DEFAULT_OPTIONS = ValidationOptions()


class UnreadableKind(StrEnum):
    """Why input could not become the target type."""

    MISSING = "missing"
    NOT_READABLE = "not_readable"
    TOO_LARGE = "too_large"
    NOT_JSON = "not_json"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class Problem:
    """One validation failure: where in the document, and what is wrong there."""

    key_path: str
    """Dotted path of the failing key; empty for the document itself."""
    message: str


@dataclass(frozen=True, slots=True)
class Unreadable:
    """Input that is not a valid document of the target type, and why."""

    kind: UnreadableKind
    source: str
    """Where the input came from: a file path, a command, a peer."""
    problems: tuple[Problem, ...]

    @property
    def key_path(self) -> str:
        """The first failing key path, or empty when the document as a whole failed."""
        return self.problems[0].key_path if self.problems else ""

    def details(self, document: str = "<document>") -> str:
        """Every problem as ``path: message``, joined; ``document`` names the empty path."""
        return "; ".join(f"{p.key_path or document}: {p.message}" for p in self.problems)

    def __str__(self) -> str:
        """The source, the kind and the details, without the input."""
        return f"{self.source}: {self.kind.value}: {self.details()}"


def _lenient_classes(schema: object) -> list[str]:
    """Every model, dataclass and TypedDict a core schema decodes that keeps unknown keys.

    Root models decode no keys of their own. Pydantic's ``BaseModel`` itself declares no
    fields: a field typed with it is an opaque slot that its owner resolves in a validator
    (for example through a registry), not a document shape, so it is not counted.
    """
    found: list[str] = []
    if isinstance(schema, dict):
        decodes_keys = not schema.get("root_model") and schema.get("cls") is not BaseModel
        if decodes_keys and schema.get("type") in ("model", "dataclass", "typed-dict"):
            config = schema.get("config") or {}
            extra = schema.get("extra_behavior") or config.get("extra_fields_behavior")
            if extra != "forbid":
                found.append(schema["cls"].__qualname__)
        for value in schema.values():
            found.extend(_lenient_classes(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_lenient_classes(value))
    return found


@lru_cache(maxsize=256)
def _require_strict(target: type[BaseModel] | TypeAdapter[Any]) -> None:
    """Refuse a target that would drop an unknown key anywhere in the document.

    Checked before any input is read, so a lenient target fails whatever the input.
    """
    schema = (
        target.core_schema if isinstance(target, TypeAdapter) else target.__pydantic_core_schema__
    )
    lenient = sorted(set(_lenient_classes(schema)))
    if lenient:
        message = (
            f"{', '.join(lenient)} must set extra='forbid' to be decoded: "
            "an unknown key in untrusted input is an error, not something to drop"
        )
        raise TypeError(message)


def _unreadable(source: str, error: ValidationError) -> Unreadable:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    problems = tuple(
        Problem(".".join(str(part) for part in detail["loc"]), detail["msg"]) for detail in details
    )
    not_json = any(detail["type"] == "json_invalid" for detail in details)
    kind = UnreadableKind.NOT_JSON if not_json else UnreadableKind.INVALID
    return Unreadable(kind, source, problems)


def _too_large(source: str, max_bytes: int) -> Unreadable:
    return Unreadable(
        UnreadableKind.TOO_LARGE, source, (Problem("", f"is larger than {max_bytes} bytes"),)
    )


@overload
def decode_json[ModelT: BaseModel](
    data: bytes | str,
    target: type[ModelT],
    *,
    source: str,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> ModelT | Unreadable: ...


@overload
def decode_json[ValueT](
    data: bytes | str,
    target: TypeAdapter[ValueT],
    *,
    source: str,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> ValueT | Unreadable: ...


def decode_json(
    data: bytes | str,
    target: type[BaseModel] | TypeAdapter[Any],
    *,
    source: str,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> object:
    """Decode ``data`` as ``target``, or return why it is not one.

    ``target`` is a ``BaseModel`` subclass, or a ``TypeAdapter`` for values that are
    not models, such as a third-party tool's output. Every model, dataclass and
    TypedDict it decodes, at any depth, must forbid unknown keys (``TypeError``
    otherwise, whatever the input: that is a programming error). ``max_bytes`` caps the input size
    (UTF-8 bytes for ``str``); ``options`` selects strict mode and validation context.
    """
    _require_strict(target)
    size = len(data) if isinstance(data, bytes) else len(data.encode("utf-8", "surrogatepass"))
    if size > max_bytes:
        return _too_large(source, max_bytes)
    try:
        if isinstance(target, TypeAdapter):
            return target.validate_json(data, strict=options.strict, context=options.context)
        return target.model_validate_json(data, strict=options.strict, context=options.context)
    except ValidationError as error:
        return _unreadable(source, error)


@overload
def read_json[ModelT: BaseModel](
    path: Path,
    target: type[ModelT],
    *,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> ModelT | Unreadable: ...


@overload
def read_json[ValueT](
    path: Path,
    target: TypeAdapter[ValueT],
    *,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> ValueT | Unreadable: ...


def read_json(
    path: Path,
    target: type[BaseModel] | TypeAdapter[Any],
    *,
    max_bytes: int,
    options: ValidationOptions = DEFAULT_OPTIONS,
) -> object:
    """Read at most ``max_bytes`` of the file at ``path`` and decode it as ``target``.

    A missing file, one that cannot be read, one past the cap, and one that is not a
    valid document each come back as an ``Unreadable`` naming ``path``.
    """
    _require_strict(target)
    source = str(path)
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except FileNotFoundError:
        return Unreadable(UnreadableKind.MISSING, source, (Problem("", "does not exist"),))
    except OSError as error:
        reason = error.strerror or type(error).__name__
        return Unreadable(UnreadableKind.NOT_READABLE, source, (Problem("", reason),))
    if len(data) > max_bytes:
        return _too_large(source, max_bytes)
    if isinstance(target, TypeAdapter):
        return decode_json(data, target, source=source, max_bytes=max_bytes, options=options)
    return decode_json(data, target, source=source, max_bytes=max_bytes, options=options)


__all__ = [
    "DEFAULT_OPTIONS",
    "Problem",
    "Unreadable",
    "UnreadableKind",
    "ValidationOptions",
    "decode_json",
    "read_json",
]
