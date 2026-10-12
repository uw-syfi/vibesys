"""Recursive immutable schema validation and canonical value serialization."""

from __future__ import annotations

import json
import weakref
from enum import Enum
from functools import cache
from hashlib import sha256
from types import UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Literal,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

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


class InstanceMemo[Fact]:
    """Remember one fact per live model instance, looked up by identity.

    Every durable write checks and serializes the whole run envelope, and each write shares
    almost all of its models with the last. Keying by value would hash the model on every
    lookup, and a pydantic model's hash walks every field recursively, which costs as much as
    the walk being saved. The identity of a live object is O(1) and cannot be reused while the
    entry exists: a weak reference evicts the entry when the instance is collected, and a hit
    must still resolve to the very same object. The facts remembered here (the verdict and
    canonical text of a frozen model) depend only on the instance's class and field values,
    which a frozen model never changes.
    """

    def __init__(self) -> None:
        self._entries: dict[int, tuple[weakref.ref[BaseModel], Fact]] = {}

    def get(self, model: BaseModel) -> Fact | None:
        """The remembered fact for this very instance, or None."""
        entry = self._entries.get(id(model))
        if entry is not None and entry[0]() is model:
            return entry[1]
        return None

    def put(self, model: BaseModel, fact: Fact) -> None:
        """Remember ``fact`` for ``model`` until it is collected."""
        key = id(model)
        entries = self._entries

        def evict(ref: weakref.ref[BaseModel]) -> None:
            current = entries.get(key)
            if current is not None and current[0] is ref:
                del entries[key]

        entries[key] = (weakref.ref(model, evict), fact)


_IMMUTABILITY_VERDICTS: InstanceMemo[bool] = InstanceMemo()


def _frozen_model_is_deeply_immutable(model: BaseModel) -> bool:
    known = _IMMUTABILITY_VERDICTS.get(model)
    if known is None:
        known = _walk_immutable(getattr(model, name) for name in _field_names(type(model)))
        _IMMUTABILITY_VERDICTS.put(model, known)
    return known


def deeply_immutable(value: object) -> bool:
    """Copied values and defaults cannot bypass registered schema guarantees."""
    return _walk_immutable((value,))


def _walk_immutable(values: Iterable[object]) -> bool:
    # Iterative: every persisted envelope is checked on each step, so the walk
    # avoids a Python call (and a generator) per leaf. An exact-type test cannot
    # match a model, tuple, frozenset or Enum, so it only skips checks that
    # would fail.
    pending = list(values)
    recall = _IMMUTABILITY_VERDICTS.get
    while pending:
        node = pending.pop()
        if type(node) in _IMMUTABLE_LEAF_TYPES:
            continue
        if isinstance(node, BaseModel):
            if not node.model_config.get("frozen"):
                return False
            known = recall(node)  # inlined: this is the hottest loop of every durable write
            if known is None:
                known = _frozen_model_is_deeply_immutable(node)
            if not known:
                return False
        elif isinstance(node, tuple | frozenset):
            pending.extend(node)
        elif not isinstance(node, Enum) or type(node.value) not in _IMMUTABLE_LEAF_TYPES:
            return False
    return True


_CANONICAL_TEXTS: InstanceMemo[str] = InstanceMemo()


def canonical_json(value: BaseModel) -> str:
    """Keep ordered sequences and canonically sort every unordered collection.

    A frozen value's text depends only on its class and fields, so it is remembered per
    instance: recovery proves the same recorded requests against their digests on every step.
    """
    if not value.model_config.get("frozen"):
        return _canonical_json(value)
    text = _CANONICAL_TEXTS.get(value)
    if text is None:
        text = _canonical_json(value)
        _CANONICAL_TEXTS.put(value, text)
    return text


def _canonical_json(value: BaseModel) -> str:
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
