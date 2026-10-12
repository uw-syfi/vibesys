"""The reply fields a turn offers, as a template variable.

The reply schema is the one source of truth for reply field names. A template that
renders text for a turn with a reply model never types a field name: it writes
``{{ reply.parent_hypothesis_id }}``, which renders as the backticked name. The caller
builds ``reply`` from the JSON Schema the turn's agent receives, so a name the schema
(trimmed by the run's capabilities) does not offer cannot be rendered: reading it raises
:class:`UnknownReplyFieldError` instead of leaving a prompt that describes a reply the
agent is not allowed to give.

Field meanings stay in the schema's field descriptions, which the agent also receives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_DEFS: Final = "$defs"
_REF_PREFIX: Final = "#/$defs/"
_SUBSCHEMA_LISTS: Final = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMAS: Final = ("items", "additionalProperties", "not")


class UnknownReplyFieldError(LookupError):
    """A template named a reply field that the turn's reply schema does not offer."""

    def __init__(self, name: str, offered: frozenset[str]) -> None:
        """Name the missing field and the fields the schema offers."""
        self.name = name
        self.offered = offered
        super().__init__(
            f"reply field {name!r} is not offered by this turn's reply schema "
            f"(offered: {', '.join(sorted(offered))})"
        )


class ReplyField:
    """One offered reply field; it renders as its backticked name."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        """Wrap an offered field name."""
        self.name = name

    def __str__(self) -> str:
        """The name in backticks, the form agents read."""
        return f"`{self.name}`"


class ReplyFields:
    """The fields a reply schema offers; attribute access yields a :class:`ReplyField`.

    Every attribute is a reply field name, so the class has no public methods that a
    field of that name could shadow; build it with :func:`reply_fields`. Iterating it
    yields the offered names.
    """

    __slots__ = ("_names",)

    def __init__(self, names: frozenset[str]) -> None:
        """Offer exactly ``names``."""
        self._names = names

    def __iter__(self) -> Iterator[str]:
        """The offered field names."""
        return iter(self._names)

    def __getattr__(self, name: str) -> ReplyField:
        """The offered field ``name``, or :class:`UnknownReplyFieldError`.

        Python protocol lookups (``__html__``, ``__deepcopy__``, ...) and the internal slot
        are not fields: they raise a plain ``AttributeError`` so ``hasattr``, ``getattr``
        with a default, ``copy`` and ``pickle`` behave normally. Ordinary unknown names do
        not raise ``AttributeError``, or Jinja would turn them into an ``Undefined``.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._names:
            raise UnknownReplyFieldError(name, self._names)
        return ReplyField(name)


def reply_fields(schema: Mapping[str, object]) -> ReplyFields:
    """The fields reachable from the root of ``schema``, the JSON Schema an agent receives.

    Definitions the root does not reach (for example a variant a run's capabilities
    removed from the schema) offer nothing. A reference to a missing definition is an
    error, since the agent would receive a schema that does not validate.
    """
    definitions = schema.get(_DEFS, {})
    if not isinstance(definitions, dict):
        message = f"schema {_DEFS} must be an object"
        raise TypeError(message)
    names: set[str] = set()
    _collect(schema, definitions, names, set())
    return ReplyFields(frozenset(names))


def _collect(
    node: object, definitions: Mapping[str, object], names: set[str], seen: set[str]
) -> None:
    if not isinstance(node, dict):
        return
    reference = node.get("$ref")
    if isinstance(reference, str):
        key = _definition_key(reference, definitions)
        if key not in seen:
            seen.add(key)
            _collect(definitions[key], definitions, names, seen)
    properties = node.get("properties")
    if isinstance(properties, dict):
        for name, sub in properties.items():
            names.add(name)
            _collect(sub, definitions, names, seen)
    for sub in _subschemas(node):
        _collect(sub, definitions, names, seen)


def _definition_key(reference: str, definitions: Mapping[str, object]) -> str:
    if not reference.startswith(_REF_PREFIX):
        message = f"unsupported schema reference {reference!r}"
        raise ValueError(message)
    key = reference.removeprefix(_REF_PREFIX)
    if key not in definitions:
        message = f"schema reference {reference!r} has no definition"
        raise ValueError(message)
    return key


def _subschemas(node: Mapping[str, object]) -> Iterator[object]:
    for key in _SUBSCHEMA_LISTS:
        group = node.get(key)
        if isinstance(group, list):
            yield from group
    for key in _SUBSCHEMAS:
        yield node.get(key)
