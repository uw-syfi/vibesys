"""Recursive immutable schema validation and canonical value serialization."""

from __future__ import annotations

import json
from enum import Enum
from types import UnionType
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import BaseModel


class ImmutableSchemaError(ValueError):
    """A registered field can hold mutable or unvalidated values."""

    def __init__(self, path: tuple[str, ...], detail: str) -> None:
        super().__init__(f"{'.'.join(path)}: {detail}")


def validate_immutable_schema(model: type[BaseModel]) -> None:
    """Reject mutable containers, open types and mutable nested models."""
    _validate_annotation(model, (), set())


def _validate_annotation(annotation: object, path: tuple[str, ...], seen: set[type]) -> None:
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        _validate_annotation(arguments[0], path, seen)
    elif origin is Literal:
        return
    elif origin in (tuple, frozenset, UnionType, Union):
        for child in arguments:
            if child is not Ellipsis:
                _validate_annotation(child, path, seen)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        _validate_model(annotation, path, seen)
    elif isinstance(annotation, type) and (
        annotation in (str, int, float, bool, bytes, type(None))
        or (
            issubclass(annotation, Enum)
            and all(
                type(member.value) in (str, int, float, bool, type(None)) for member in annotation
            )
        )
    ):
        return
    else:
        raise ImmutableSchemaError(path, "immutable closed field type required")


def _validate_model(model: type[BaseModel], path: tuple[str, ...], seen: set[type]) -> None:
    if model in seen:
        return
    seen.add(model)
    config = model.model_config
    if not (config.get("frozen") and config.get("strict") and config.get("extra") == "forbid"):
        raise ImmutableSchemaError(path, "strict immutable value model required")
    for name, field in model.model_fields.items():
        _validate_annotation(field.annotation, (*path, name), seen)


def canonical_json(value: BaseModel) -> str:
    """Keep ordered sequences and canonically sort every unordered collection."""
    serialized = value.model_dump(mode="json", serialize_as_any=True)
    return json.dumps(
        _canonical(value, serialized), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical(value: object, serialized: object) -> object:
    if isinstance(value, BaseModel) and isinstance(serialized, dict):
        names = {name: name for name in type(value).model_fields}
        names.update(
            {
                field.serialization_alias or field.alias or name: name
                for name, field in type(value).model_fields.items()
            }
        )
        return {
            name: _canonical(getattr(value, names[name]), child) if name in names else child
            for name, child in serialized.items()
        }
    if isinstance(value, tuple | frozenset) and isinstance(serialized, list):
        children = [_canonical(child, wire) for child, wire in zip(value, serialized, strict=True)]
        if isinstance(value, frozenset):
            return sorted(children, key=lambda child: json.dumps(child, sort_keys=True))
        return children
    return serialized
