"""Recursive immutable schema validation and canonical value serialization."""

from __future__ import annotations

import json
from enum import Enum
from functools import cache, lru_cache
from hashlib import sha256
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Literal, TypeAliasType, Union, get_args, get_origin

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterable


class ImmutableSchemaError(ValueError):
    """A registered field can hold mutable or unvalidated values."""

    def __init__(self, path: tuple[str, ...], detail: str) -> None:
        super().__init__(f"{'.'.join(path)}: {detail}")


@cache
def _validated_schema(model: type[BaseModel]) -> None:
    # A class's fields and config are fixed once it is defined, so a verdict of "valid" holds for
    # the rest of the process. `cache` stores only returns: an invalid model raises every time.
    _validate_annotation(model, (), set())


def validate_immutable_schema(model: type[BaseModel]) -> None:
    """Reject mutable containers, open types and mutable nested models."""
    _validated_schema(model)


def _validate_annotation(annotation: object, path: tuple[str, ...], seen: set[type]) -> None:
    if isinstance(annotation, TypeAliasType):
        annotation = annotation.__value__
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        _validate_annotation(arguments[0], path, seen)
    elif origin is Literal:
        if not all(
            type(child.value if isinstance(child, Enum) else child)
            in (str, int, float, bool, type(None))
            for child in arguments
        ):
            raise ImmutableSchemaError(path, "immutable primitive Literal required")
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
    aliases = [
        field.serialization_alias or field.alias or name
        for name, field in model.model_fields.items()
    ]
    if config.get("serialize_by_alias") and len(set(aliases)) != len(aliases):
        raise ImmutableSchemaError(path, "ambiguous serialization aliases")
    for name, field in model.model_fields.items():
        if (
            not field.is_required()
            and field.default_factory is None
            and not deeply_immutable(field.default)
        ):
            raise ImmutableSchemaError((*path, name), "mutable schema default")
        if field.default_factory is not None and not config.get("validate_default"):
            raise ImmutableSchemaError((*path, name), "default factory requires validate_default")
        _validate_annotation(field.annotation, (*path, name), seen)


_IMMUTABLE_LEAF_TYPES = frozenset({str, int, float, bool, bytes, type(None)})


@cache
def _field_names(model: type[BaseModel]) -> tuple[str, ...]:
    return tuple(model.model_fields)


@cache
def _wire_names(model: type[BaseModel]) -> dict[str, str]:
    """Map each serialized field name to its attribute name; callers must not mutate it."""
    by_alias = model.model_config.get("serialize_by_alias")
    return {
        (field.serialization_alias or field.alias or name) if by_alias else name: name
        for name, field in model.model_fields.items()
    }


#: Models whose immutability verdict is remembered, by value. Every durable write checks the
#: whole run envelope, and each write shares almost all of its models with the last, so without
#: this the check repeats the walk over the unchanged history on every step. The verdict of a
#: frozen model depends only on its class and field values, which its hash and equality cover.
_REMEMBERED_MODELS = 65536


@lru_cache(maxsize=_REMEMBERED_MODELS)
def _frozen_model_is_deeply_immutable(model: BaseModel) -> bool:
    return _walk_immutable(getattr(model, name) for name in _field_names(type(model)))


def deeply_immutable(value: object) -> bool:
    """Copied values and defaults cannot bypass registered schema guarantees."""
    return _walk_immutable((value,))


def _walk_immutable(values: Iterable[object]) -> bool:
    # Iterative: every persisted envelope is checked on each step, so the walk
    # avoids a Python call (and a generator) per leaf. An exact-type test cannot
    # match a model, tuple, frozenset or Enum, so it only skips checks that
    # would fail.
    pending = list(values)
    while pending:
        node = pending.pop()
        if type(node) in _IMMUTABLE_LEAF_TYPES:
            continue
        if isinstance(node, BaseModel):
            if not node.model_config.get("frozen"):
                return False
            try:
                known = _frozen_model_is_deeply_immutable(node)
            except TypeError:
                # Unhashable: a field holds a list, dict or set, which is mutable.
                return False
            if not known:
                return False
        elif isinstance(node, tuple | frozenset):
            pending.extend(node)
        elif not isinstance(node, Enum) or type(node.value) not in _IMMUTABLE_LEAF_TYPES:
            return False
    return True


def canonical_json(value: BaseModel) -> str:
    """Keep ordered sequences and canonically sort every unordered collection."""
    serialized = value.model_dump(mode="json", serialize_as_any=True)
    return json.dumps(
        _canonical(value, serialized), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical(value: object, serialized: object) -> object:
    if type(value) in _IMMUTABLE_LEAF_TYPES:
        return serialized
    if isinstance(value, BaseModel) and isinstance(serialized, dict):
        names = _wire_names(type(value))
        result: dict[str, object] = {}
        for name, child in serialized.items():
            attribute = names.get(name)
            if attribute is None:
                result[name] = child
                continue
            member = getattr(value, attribute)
            result[name] = (
                child if type(member) in _IMMUTABLE_LEAF_TYPES else _canonical(member, child)
            )
        return result
    if isinstance(value, tuple | frozenset) and isinstance(serialized, list):
        children = [
            wire if type(child) in _IMMUTABLE_LEAF_TYPES else _canonical(child, wire)
            for child, wire in zip(value, serialized, strict=True)
        ]
        if isinstance(value, frozenset):
            return sorted(children, key=lambda child: json.dumps(child, sort_keys=True))
        return children
    return serialized


def digest(value: BaseModel) -> str:
    """Deterministic value fingerprint, with no clock or random identity source."""
    return sha256(canonical_json(value).encode()).hexdigest()
