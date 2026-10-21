"""Recursive immutable schema validation and canonical value serialization."""

from __future__ import annotations

import json
import re
import weakref
from dataclasses import fields
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
    cast,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ConfigDict, TypeAdapter

from vs_core._corevalue import CoreValue, FrozenMap

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


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
    elif origin in (tuple, frozenset, FrozenMap, UnionType, Union):
        for child in arguments:
            if child is not Ellipsis:
                _validate_annotation(child, path, seen)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel | CoreValue):
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


def _validate_model(
    model: type[BaseModel] | type[CoreValue], path: tuple[str, ...], seen: set[type]
) -> None:
    if model in seen:
        return
    seen.add(model)
    if issubclass(model, CoreValue):
        for member in fields(model):
            _validate_annotation(
                model.__pydantic_fields__[member.name].annotation, (*path, member.name), seen
            )
        return
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


_read_slot = object.__getattribute__
_write_slot = object.__setattr__
_MEMO_SLOTS = frozenset({"_text", "_immutable", "_canonical"})


def recall[Fact](model: BaseModel, slot: str) -> Fact | None:
    """The fact kept in ``slot`` of this very instance, or None.

    A value model owns slots for the facts derived from its fields (see `Value`), so a lookup is
    an attribute read: no side table, no weak reference. A slot is empty on a fresh object,
    and `object.__getattribute__` reports that without pydantic's slower `__getattr__`. A model
    that has no such slot (any other pydantic model) simply remembers nothing.
    """
    try:
        return _read_slot(model, slot)
    except AttributeError:
        return None


@cache
def _memoizes(model_type: type[BaseModel]) -> bool:
    """Whether instances of this class have the slots that hold remembered facts."""
    declared: set[str] = set()
    for cls in model_type.__mro__:
        slots = getattr(cls, "__slots__", ())
        declared.update((slots,) if isinstance(slots, str) else slots)
    return declared >= _MEMO_SLOTS


def keep(model: BaseModel, slot: str, fact: object) -> None:
    """Remember ``fact`` in ``slot`` of this instance, if its class has the slot."""
    if _memoizes(type(model)):
        _write_slot(model, slot, fact)


def _frozen_model_is_deeply_immutable(model: BaseModel) -> bool:
    known = recall(model, "_immutable")
    if known is None:
        known = _judge_immutable(model)
    return known


def _judge_immutable(model: BaseModel) -> bool:
    """Walk a frozen model whose verdict is not yet kept, and keep the verdict."""
    known = _walk_immutable(model.__dict__.values())
    keep(model, "_immutable", known)
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
    while pending:
        node = pending.pop()
        if type(node) in _IMMUTABLE_LEAF_TYPES:
            continue
        try:
            # A verdict is only ever kept for a frozen model, so a kept one settles this node.
            verdict = _read_slot(node, "_immutable")  # inlined: the hottest loop of a write
        except AttributeError:
            pass
        else:
            if verdict:
                continue
            return False
        if isinstance(node, BaseModel):
            if not node.model_config.get("frozen") or not _judge_immutable(node):
                return False
        elif isinstance(node, CoreValue | FrozenMap | tuple | frozenset):
            pending.extend(_immutable_children(node))
        elif not isinstance(node, Enum) or type(node.value) not in _IMMUTABLE_LEAF_TYPES:
            return False
    return True


def _immutable_children(node: CoreValue | FrozenMap | tuple | frozenset) -> Iterable[object]:
    """Children of the closed immutable container representations."""
    if isinstance(node, CoreValue):
        return (getattr(node, member.name) for member in fields(node))
    if isinstance(node, FrozenMap):
        return (*node.keys(), *node.values())
    return node


def canonical_json(value: BaseModel) -> str:
    """Keep ordered sequences and canonically sort every unordered collection.

    A frozen value's text depends only on its class and fields, so it is remembered per
    instance: recovery proves the same recorded requests against their digests on every step.
    """
    if not value.model_config.get("frozen"):
        return _canonical_json(value)
    text: str | None = recall(value, "_canonical")
    if text is None:
        text = _canonical_json(value)
        keep(value, "_canonical", text)
    return text


def _canonical_json(value: BaseModel) -> str:
    serialized = value.model_dump(mode="json", serialize_as_any=True)
    return json.dumps(
        _canonical(value, serialized), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical(value: object, serialized: object) -> object:
    if type(value) in _IMMUTABLE_LEAF_TYPES:
        return serialized
    if isinstance(value, CoreValue) and isinstance(serialized, dict):
        return {name: _canonical(getattr(value, name), child) for name, child in serialized.items()}
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


_FRAGMENT_TOKEN = re.compile(r'"\\u0000F([0-9]+)\\u0000"')
_FRAGMENT_DATA: TypeAdapter[object] = TypeAdapter(
    object, config=ConfigDict(ser_json_inf_nan="null")
)


class _Fragments:
    """What one `encode_json` call collects while pydantic walks the model.

    Pydantic calls `emit_fragment` for every frozen value. A value whose JSON text is remembered
    is replaced by a token and never walked; any other is serialized into plain data in which
    its own children are tokens already. The tokens are spliced back after pydantic has written
    the document, so the bytes are the ones pydantic writes and nothing else needs to know its
    rules for aliases, field serializers, enums, floats or escaping.
    """

    def __init__(self) -> None:
        self.seen: list[BaseModel] = []
        self.texts: list[str] = []  # "" until a value serialized in this call is resolved
        self.pending: list[tuple[int, BaseModel, object]] = []
        self.used = 0

    def emit_new(self, model: BaseModel, handler: Callable[[BaseModel], object]) -> object:
        """A value whose text is not remembered: plain data in this call's tokens, or as it is."""
        if not (model.model_config.get("frozen") and _text_is_position_independent(type(model))):
            return handler(model)
        self.seen.append(model)
        data = handler(model)  # children are tokens, so they were all emitted before this
        self.pending.append((len(self.texts), model, data))
        self.texts.append("")
        return f"\x00F{len(self.texts) - 1}\x00"

    def expand(self, template: str) -> str:
        """The template with every token replaced by the text it stands for."""
        parts = _FRAGMENT_TOKEN.split(template)
        if len(parts) == 1:
            return template
        pieces = [parts[0]]
        for position in range(1, len(parts), 2):
            pieces.append(self.texts[int(parts[position])])
            pieces.append(parts[position + 1])
        self.used += len(parts) // 2
        return "".join(pieces)

    def resolve(self, document: str) -> str | None:
        """The finished text, or None when the tokens did not all appear exactly once.

        A string in the data that spells a token would be spliced as if it were one; counting
        each token's single use catches it, and the caller then writes the plain encoding.
        """
        try:
            for index, _, data in self.pending:
                self.texts[index] = self.expand(_FRAGMENT_DATA.dump_json(data).decode())
            text = self.expand(document)
        except IndexError:
            return None
        return text if self.used == len(self.texts) else None

    def remember(self) -> None:
        """Keep the text of every value this call serialized, for the next call."""
        for index, model, _ in self.pending:
            if _frozen_model_is_deeply_immutable(model):
                keep(model, "_text", self.texts[index])


@cache
def _text_is_position_independent(model: type[BaseModel]) -> bool:
    """Whether an instance's text is the same under every field that can hold it.

    Pydantic writes a field by the schema of its declared class, so an instance of a class
    that extends another model may be written with fewer fields where it sits in a field
    declared as that other model, and in full where it sits in a field of its own class. Which
    fields can hold it is not known here (and `model_copy` can put it anywhere), so such an
    instance's text is not remembered and pydantic writes it every time. The answer depends only
    on the class.
    """
    return not any(
        base.model_fields
        for base in model.__mro__[1:]
        if isinstance(base, type) and issubclass(base, BaseModel)
    )


def emit_fragment(
    model: BaseModel, handler: Callable[[BaseModel], object], context: object
) -> object:
    """Serializer hook of every frozen value: a token inside `encode_json`, else plain data."""
    if type(context) is not _Fragments:
        return handler(model)
    try:
        text = _read_slot(model, "_text")  # inlined: one call per value of every durable write
    except AttributeError:
        return context.emit_new(model, handler)
    context.seen.append(model)
    context.texts.append(text)
    return f"\x00F{len(context.texts) - 1}\x00"


class EncodingMismatchError(ValueError):
    """Spliced fragments differ from the plain encoding, so a remembered text is not the value's."""

    def __init__(self, culprits: tuple[str, ...]) -> None:
        super().__init__(
            "encode_json differs from model_dump_json; a value's text depends on where it sits "
            f"(a subclass in a superclass-typed field without SerializeAsAny?): {', '.join(culprits)}"
        )


class _Verification:
    """Whether `encode_json` also checks itself against the plain encoding.

    Only the byte-identity tests turn this on: checking writes every value a second time.
    Product code and the rest of the suite keep the fast path, and the mechanism it guards
    against is closed statically (tests/quality/test_value_fields_name_exact_types.py).
    """

    enabled = False


def verify_encoding(*, enabled: bool) -> None:
    """Make `encode_json` compare its bytes with the plain encoding and raise on a difference."""
    _Verification.enabled = enabled


def _mismatch(fragments: _Fragments, model: BaseModel) -> EncodingMismatchError:
    """Name the types whose own plain text differs from the text spliced for them."""
    culprits = {
        type(seen).__name__
        for seen in fragments.seen
        if (text := recall(seen, "_text")) is not None and text != seen.model_dump_json()
    }
    return EncodingMismatchError(tuple(sorted(culprits)) or (type(model).__name__,))


def encode_json(model: BaseModel) -> bytes:
    """The bytes of ``model.model_dump_json()``, reusing the text of unchanged frozen values.

    Each durable write encodes the whole run, and consecutive writes share almost every
    sub-tree as the identical object. The text of a frozen value depends only on its class
    and field values, so it is remembered per instance (see `InstanceMemo`) and a value that
    came through unchanged is not walked or encoded again: only values built since the last
    call are. The output is byte-for-byte the plain encoding, which is also what is written if
    the fragments cannot be proven to splice back exactly.
    """
    fragments = _Fragments()
    text = fragments.resolve(model.model_dump_json(context=fragments))
    if text is None:
        return model.model_dump_json().encode()
    if _Verification.enabled and text != model.model_dump_json():
        raise _mismatch(fragments, model)
    fragments.remember()
    return text.encode()


def share_unchanged[V](old: V, new: V) -> V:
    """``new``, except that every part equal to the same part of ``old`` is the object in ``old``.

    Models are compared field by field and tuples position by position, so only the parts that
    were rebuilt are walked: a part that is already the very object in ``old`` is skipped at
    once. Anything that is neither (a frozenset, a number, a string) is kept as ``new`` has it.
    The result equals ``new``.
    """
    return cast("V", _share(old, new))


def _share(old: object, new: object) -> object:
    if old is new:
        return new
    if isinstance(new, BaseModel) and type(old) is type(new):
        return _share_model(old, new)
    if type(old) is tuple and type(new) is tuple:
        return _share_tuple(old, new)
    return new


def _share_model(old: BaseModel, new: BaseModel) -> BaseModel:
    before_fields = old.__dict__
    after_fields = new.__dict__
    every_part_is_old = True
    changed: dict[str, object] | None = None
    for name in _field_names(type(new)):
        before = before_fields[name]
        after = after_fields[name]
        if before is after:
            continue
        part = _share(before, after)
        if part is not before:
            every_part_is_old = False
        if part is not after:
            if changed is None:
                changed = {}
            changed[name] = part
    if every_part_is_old:
        return old
    return new.model_copy(update=changed) if changed else new


def _share_tuple(old: tuple[object, ...], new: tuple[object, ...]) -> tuple[object, ...]:
    parts = tuple(_share(before, after) for before, after in zip(old, new, strict=False))
    if len(new) == len(old) and all(
        part is before for part, before in zip(parts, old, strict=True)
    ):
        return old
    if all(part is after for part, after in zip(parts, new, strict=False)):
        return new
    return (*parts, *new[len(parts) :])
