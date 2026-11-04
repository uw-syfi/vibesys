"""Bounded retained documents and diagnostics for the pure semantic importer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from hashlib import sha256
from math import isfinite
from typing import TYPE_CHECKING

from pydantic import BaseModel

from vs_decode.api import Unreadable, ValidationOptions, decode_json
from vs_proto.api import imported_facts as pb

if TYPE_CHECKING:
    from pydantic import TypeAdapter

    from vs_core.api import OperationRegistry

MAX_SOURCE_BYTES = 128 * 1024 * 1024


SourceFormat = pb.ImportFormat
"""The retained read formats declared by the shared import contract."""


class FactImportError(ValueError):
    """A prerequisite diagnostic; never authorizes dispatch or declares a run final."""

    def __init__(self, path: str, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(f"{path}: {detail}")


@dataclass(frozen=True, slots=True)
class ImportSource:
    """Exact bytes from one retained document, with its explicit read format."""

    path: str
    format: SourceFormat
    data: bytes

    def provenance(self, selector: str) -> pb.ImportedFrom:
        return pb.ImportedFrom(
            path=self.path,
            sha256=sha256(self.data).hexdigest(),
            selector=selector,
            format=self.format,
        )


def decode[T](
    source: ImportSource,
    model: TypeAdapter[T],
    registry: OperationRegistry,
    *,
    selector: str = "$",
    data: bytes | str | None = None,
) -> T:
    """Strict JSON decoding, including duplicate keys and errors inside embedded JSON."""
    result = decode_json(
        source.data if data is None else data,
        model,
        source=f"{source.path}:{selector}",
        max_bytes=MAX_SOURCE_BYTES,
        options=ValidationOptions(
            context={"operation_registry": registry, "persisted_operation": True}
        ),
    )
    if isinstance(result, Unreadable):
        raise FactImportError(f"{source.path}:{selector}", result.details())
    ensure_finite(source, selector, result)
    return result


def ensure_finite(source: ImportSource, selector: str, value: object) -> None:
    pending = [(selector, value)]
    while pending:
        path, item = pending.pop()
        if isinstance(item, float) and not isfinite(item):
            raise FactImportError(f"{source.path}:{path}", "nonfinite value")
        if isinstance(item, BaseModel):
            pending.extend(
                (f"{path}.{name}", getattr(item, name)) for name in type(item).model_fields
            )
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(
                (f"{path}.{field.name}", getattr(item, field.name)) for field in fields(item)
            )
        elif isinstance(item, Mapping):
            pending.extend((f"{path}.{key}", child) for key, child in item.items())
        elif isinstance(item, tuple | list):
            pending.extend((f"{path}[{index}]", child) for index, child in enumerate(item))
