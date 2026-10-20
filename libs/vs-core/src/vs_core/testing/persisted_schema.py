"""Readable snapshots of persisted shapes, and the check that a shape change keeps old documents.

A persisted shape (the run envelope, the runtime record, a strategy's checkpoint) is pinned by a
snapshot of its JSON schema: a ``shape`` and a ``version`` line, then one sorted line per class
(``Name {...}``) and per field (``Name.field required {...}``). Titles and descriptions are left
out, so a docstring edit is no shape change. Two branches that change different fields change
different lines, and Git merges them.

A change of a shape keeps its version only when every document saved before it still decodes
after it (`schema_incompatibilities`): a new field with a default, a closed set or a union that
gains a member, a looser bound, a renamed class. Anything else (a removed, retyped or newly
required field, a changed default, a tighter bound, a closed set that loses a member, a union's
discriminator added, removed or renamed, a tag that selects another class, a new branch of an
undiscriminated union that also takes an old object) raises the version, with a migration where
the shape has a chain (`snapshot_problems`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping

type Schema = dict[str, object]

_DROPPED: Final = frozenset({"title", "description", "examples"})
"""Keywords that say nothing about which documents a schema accepts."""
_JSON: Final = TypeAdapter(JsonValue)
_ROOT: Final = "$root"
_DEFS: Final = "#/$defs/"
_LOWER: Final = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
_UPPER: Final = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
_EXACT: Final = ("pattern", "format", "multipleOf", "uniqueItems", "propertyNames")
_HANDLED: Final = frozenset(
    {
        *_DROPPED,
        *_LOWER,
        *_UPPER,
        *_EXACT,
        "$ref",
        "type",
        "enum",
        "const",
        "anyOf",
        "oneOf",
        "allOf",
        "discriminator",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "prefixItems",
        "default",
    }
)
_BUMP: Final = "bump the version (and register a migration where the shape has a chain)"


@dataclass(frozen=True)
class Incompatibility:
    """One change of a persisted shape that a document saved before it may not survive."""

    path: str
    """The field, as property names from the root (``core.evaluation.comparisons[].verdict``)."""
    rule: str

    def __str__(self) -> str:
        """The path and the rule, as a check reports them."""
        return f"{self.path or '<root>'}: {self.rule}"


@dataclass(frozen=True)
class SchemaSnapshot:
    """A parsed snapshot: the shape's name, its version and its JSON schema."""

    shape: str
    version: tuple[int, ...]
    schema: Schema


def schema_snapshot(shape: str, version: tuple[int, ...], schema: Mapping[str, object]) -> str:
    """The snapshot text of ``schema``: header lines, then one sorted line per class and field."""
    stripped = _as_dict(_strip(dict(schema)))
    definitions = _as_dict(stripped.pop("$defs", {}))
    lines: list[str] = []
    for name, node in [(_ROOT, stripped), *definitions.items()]:
        _check_name(name, "class")
        lines += _class_lines(name, _as_dict(node))
    header = [
        f"# The persisted shape {shape}, one line per class and field. Generated: rerun its test",
        "# with UPDATE_GOLDEN=1. scripts/check_persisted_schema_compat.py checks a change keeps",
        "# every saved document, or raises the version.",
        f"shape {shape}",
        f"version {'.'.join(map(str, version))}",
    ]
    return "\n".join([*header, *sorted(lines)]) + "\n"


def assert_schema_snapshot(golden: str, expected: str) -> None:
    """Fail unless the committed snapshot ``golden`` is the ``expected`` one, naming the lines."""
    if golden == expected:
        return
    before, after = set(golden.splitlines()), set(expected.splitlines())
    gone = "\n  ".join(sorted(before - after)[:20])
    new = "\n  ".join(sorted(after - before)[:20])
    message = (
        "the persisted shape is not the one its snapshot pins; rerun this test with "
        f"UPDATE_GOLDEN=1 and commit the snapshot.\nLines gone:\n  {gone}\nLines new:\n  {new}"
    )
    raise AssertionError(message)


def parse_schema_snapshot(text: str) -> SchemaSnapshot:
    """The snapshot ``text`` read back; a malformed line raises ``ValueError`` naming it."""
    shape, version = "", ()
    classes: dict[str, Schema] = {}
    fields: dict[str, dict[str, Schema]] = {}
    required: dict[str, list[str]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        location, _, rest = line.partition(" ")
        marker, _, body = rest.partition(" ")
        try:
            if location == "shape":
                shape = rest
            elif location == "version":
                version = tuple(int(part) for part in rest.split("."))
            elif "." in location and marker in {"required", "optional"}:
                name, _, field = location.partition(".")
                fields.setdefault(name, {})[field] = _as_dict(_JSON.validate_json(body))
                required.setdefault(name, []).extend([field] if marker == "required" else [])
            else:
                classes[location] = _as_dict(_JSON.validate_json(rest))
        except ValueError as error:
            message = f"line {number} of the snapshot is malformed: {line[:80]!r}"
            raise ValueError(message) from error
    if not shape or not version:
        message = "the snapshot has no 'shape' or 'version' line"
        raise ValueError(message)
    return SchemaSnapshot(shape, version, _assemble(classes, fields, required))


def _assemble(
    classes: dict[str, Schema], fields: dict[str, dict[str, Schema]], required: dict[str, list[str]]
) -> Schema:
    """The JSON schema of a snapshot's class and field lines, its root holding the classes."""
    for name, properties in fields.items():
        node = classes.setdefault(name, {})
        node["properties"] = properties
        if required.get(name):
            node["required"] = sorted(required[name])
    root = classes.pop(_ROOT, {})
    if classes:
        root["$defs"] = classes
    return root


def schema_incompatibilities(
    old: Mapping[str, object], new: Mapping[str, object]
) -> tuple[Incompatibility, ...]:
    """Each change from ``old`` to ``new`` that some document valid under ``old`` may not survive.

    Classes are matched by structure from the root, not by name, so a rename is no change.
    """
    return tuple(dict.fromkeys(_Compare(dict(old), dict(new)).run()))


def snapshot_problems(base: str | None, head: str | None, path: str) -> tuple[str, ...]:
    """What is wrong with the snapshot at ``path`` changing from ``base`` to ``head``.

    A new snapshot is fine; a dropped one, or one whose version went down, is not. Under the
    same version every change must keep the documents ``base`` describes; a raised version
    allows any change.
    """
    if base is None:
        return ()
    if head is None:
        return (f"{path}: the snapshot was deleted; a persisted shape keeps its snapshot",)
    old, new = parse_schema_snapshot(base), parse_schema_snapshot(head)
    if new.version < old.version:
        return (f"{path}: the version was lowered from {old.version} to {new.version}",)
    if new.version > old.version:
        return ()
    version = ".".join(map(str, new.version))
    return tuple(
        f"{path}: {new.shape} {problem} under version {version}; {_BUMP}"
        for problem in schema_incompatibilities(old.schema, new.schema)
    )


def _strip(node: object, *, names: bool = False) -> object:
    """``node`` without the keywords in ``_DROPPED``; ``names`` marks a map of names to schemas."""
    if isinstance(node, list):
        return [_strip(item) for item in node]
    if not isinstance(node, dict):
        return node
    if names:
        return {key: _strip(value) for key, value in node.items()}
    return {
        key: _strip(value, names=key in {"properties", "$defs", "mapping"})
        for key, value in node.items()
        if key not in _DROPPED
    }


def _class_lines(name: str, node: Schema) -> list[str]:
    properties = _as_dict(node.get("properties"))
    if not properties:
        return [f"{name} {_json(node)}"]
    required = set(_as_list(node.get("required")))
    rest = {key: value for key, value in node.items() if key not in {"properties", "required"}}
    lines = [f"{name} {_json(rest)}"]
    for field, schema in properties.items():
        _check_name(field, f"field of {name}")
        marker = "required" if field in required else "optional"
        lines.append(f"{name}.{field} {marker} {_json(schema)}")
    return lines


def _check_name(name: str, what: str) -> None:
    if not name or any(char.isspace() or char == "." for char in name):
        message = f"the {what} {name!r} cannot be a snapshot location (a space or a dot)"
        raise ValueError(message)


def _as_dict(value: object) -> Schema:
    """``value`` if it is an object; a schema keyword of another JSON type reads as empty."""
    return value if isinstance(value, dict) else {}


def _tighter(old: object, new: object, sign: int) -> bool:
    """Whether bound ``new`` admits less than ``old`` (none); ``sign`` 1 for a lower bound."""
    if not isinstance(old, int | float) or not isinstance(new, int | float):
        return old != new
    return sign * (new - old) > 0


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _types(node: Schema) -> set[str] | None:
    kind = node.get("type")
    if kind is None:
        return None
    return {kind} if isinstance(kind, str) else {str(item) for item in _as_list(kind)}


def _values(node: Schema) -> list[object] | None:
    if "const" in node:
        return [node["const"]]
    enum = node.get("enum")
    return list(enum) if isinstance(enum, list) else None


class _Compare:
    """One structural walk of an old and a new schema, from their roots."""

    def __init__(self, old: Schema, new: Schema) -> None:
        self._old_defs = _as_dict(old.get("$defs"))
        self._new_defs = _as_dict(new.get("$defs"))
        self._old = {k: v for k, v in old.items() if k != "$defs"}
        self._new = {k: v for k, v in new.items() if k != "$defs"}
        self._seen: dict[tuple[str, str], list[Incompatibility]] = {}

    def run(self) -> list[Incompatibility]:
        """Every incompatibility found from the roots down, in walk order."""
        return self._node(self._old, self._new, "")

    def takes(self, old: object, new: object) -> bool:
        """Whether every document valid under the old schema ``old`` is valid under ``new``."""
        return not self._node(old, new, "")

    def _node(self, old_raw: object, new_raw: object, path: str) -> list[Incompatibility]:
        old, new = _clean(old_raw), _clean(new_raw)
        found: list[Incompatibility] = []
        if "default" in old and "default" in new and old["default"] != new["default"]:
            found.append(Incompatibility(path, f"the default changed from {old['default']!r}"))
        old_ref, new_ref = _ref(old), _ref(new)
        if (
            old_ref is not None
            and new_ref is not None
            and len(old) == len(new) == 1 + ("default" in old)
        ):
            key = (old_ref, new_ref)
            if key not in self._seen:
                self._seen[key] = []
                self._seen[key] = self._compare(
                    _clean(self._old_defs.get(old_ref)),
                    _clean(self._new_defs.get(new_ref)),
                    path,
                )
            return found + self._seen[key]
        return found + self._compare(
            _expand(old, self._old_defs), _expand(new, self._new_defs), path
        )

    def _compare(self, old: Schema, new: Schema, path: str) -> list[Incompatibility]:
        if old.get("anyOf") or old.get("oneOf") or new.get("anyOf") or new.get("oneOf"):
            return self._union(old, new, path)
        old_types, new_types = _types(old), _types(new)
        if new_types is not None:
            accepted = new_types | ({"integer"} if "number" in new_types else set())
            if old_types is None or not old_types <= accepted:
                return [Incompatibility(path, f"the type narrowed from {old_types} to {new_types}")]
        return [
            *self._closed_set(old, new, path),
            *self._limits(old, new, path),
            *self._object(old, new, path),
            *self._array(old, new, path),
            *(
                Incompatibility(path, f"the keyword {key!r} changed")
                for key in sorted((set(old) | set(new)) - _HANDLED)
                if old.get(key) != new.get(key)
            ),
        ]

    def _union(self, old: Schema, new: Schema, path: str) -> list[Incompatibility]:
        """Every branch of the old union must be taken by some branch of the new one.

        A discriminated union picks its branch by one property's value, so that property and
        the branch each old tag selects must stay; a new tag is a new member. An undiscriminated
        union picks the branch that fits best, so a new branch must not also take an old one.
        """
        old_branches = _branches(old)
        new_branches = _branches(new)
        found = self._discriminator(old.get("discriminator"), new.get("discriminator"), path)
        for branch in old_branches:
            trials = [self._node(branch, candidate, path) for candidate in new_branches]
            if not all(trials):
                continue
            closest = min(trials, key=len)
            described = _json(branch)[:60]
            found += closest or [Incompatibility(path, f"no branch takes the old {described}")]
        if "discriminator" not in old and "discriminator" not in new:
            found += self._ambiguity(old_branches, new_branches, path)
        return found

    def _discriminator(self, old: object, new: object, path: str) -> list[Incompatibility]:
        """A changed discriminator property, or a lost or retargeted tag, of a union."""
        if old is None and new is None:
            return []
        if old is None or new is None:
            change = "added" if old is None else "removed"
            return [Incompatibility(path, f"the union's discriminator was {change}")]
        old_property = _as_dict(old).get("propertyName")
        new_property = _as_dict(new).get("propertyName")
        if old_property != new_property:
            rule = f"the discriminator changed from {old_property!r} to {new_property!r}"
            return [Incompatibility(path, rule)]
        old_tags = _as_dict(_as_dict(old).get("mapping"))
        new_tags = _as_dict(_as_dict(new).get("mapping"))
        found: list[Incompatibility] = []
        for tag, target in old_tags.items():
            if tag not in new_tags:
                found.append(Incompatibility(path, f"the discriminator lost the tag {tag!r}"))
            else:
                found += self._node({"$ref": target}, {"$ref": new_tags[tag]}, path)
        return found

    def _ambiguity(
        self, old_branches: list[object], new_branches: list[object], path: str
    ) -> list[Incompatibility]:
        """Each old class branch that more new class branches take than old ones did.

        Pydantic decodes an undiscriminated union's object into the branch that fits it best,
        so a second branch that also takes an old document may decode it as another class.
        """
        old_objects = [b for b in old_branches if _is_object(_expand(_clean(b), self._old_defs))]
        new_objects = [b for b in new_branches if _is_object(_expand(_clean(b), self._new_defs))]
        if len(new_objects) <= 1:
            return []
        within_old = _Compare({"$defs": self._old_defs}, {"$defs": self._old_defs})
        found: list[Incompatibility] = []
        for branch in old_objects:
            before = sum(within_old.takes(branch, other) for other in old_objects)
            after = sum(self.takes(branch, candidate) for candidate in new_objects)
            if after > before:
                described = _json(branch)[:60]
                rule = f"another branch now also takes the old {described}, and may decode it"
                found.append(Incompatibility(path, rule))
        return found

    def _closed_set(self, old: Schema, new: Schema, path: str) -> list[Incompatibility]:
        new_values = _values(new)
        if new_values is None:
            return []
        old_values = _values(old)
        if old_values is None:
            return [Incompatibility(path, f"the value is now restricted to {new_values}")]
        lost = [value for value in old_values if value not in new_values]
        return [Incompatibility(path, f"the closed set lost {lost}")] if lost else []

    def _limits(self, old: Schema, new: Schema, path: str) -> list[Incompatibility]:
        found: list[Incompatibility] = []
        for keys, sign in ((_LOWER, 1), (_UPPER, -1)):
            found.extend(
                Incompatibility(path, f"{key} tightened to {new[key]!r}")
                for key in keys
                if key in new and _tighter(old.get(key), new[key], sign)
            )
        found.extend(
            Incompatibility(path, f"{key} changed to {new[key]!r}")
            for key in _EXACT
            if key in new and old.get(key) != new[key]
        )
        return found

    def _object(self, old: Schema, new: Schema, path: str) -> list[Incompatibility]:
        found: list[Incompatibility] = []
        old_fields = _as_dict(old.get("properties"))
        new_fields = _as_dict(new.get("properties"))
        newly = set(_as_list(new.get("required"))) - set(_as_list(old.get("required")))
        found.extend(
            Incompatibility(
                _join(path, str(field)),
                "the field became required"
                if field in old_fields
                else "a new field has no default",
            )
            for field in sorted(map(str, newly))
        )
        for field, schema in old_fields.items():
            if field not in new_fields:
                found.append(Incompatibility(_join(path, field), "the field was removed"))
            else:
                found += self._node(schema, new_fields[field], _join(path, field))
        old_extra, new_extra = old.get("additionalProperties"), new.get("additionalProperties")
        if new_extra is False and old_extra is not False:
            found.append(Incompatibility(path, "extra keys are no longer accepted"))
        elif isinstance(new_extra, dict) and old_extra is not False:
            found += self._node(_as_dict(old_extra), new_extra, f"{path}{{}}")
        return found

    def _array(self, old: Schema, new: Schema, path: str) -> list[Incompatibility]:
        found: list[Incompatibility] = []
        if "items" in new:
            found += self._node(old.get("items", {}), new["items"], f"{path}[]")
        old_prefix, new_prefix = old.get("prefixItems"), new.get("prefixItems")
        if isinstance(new_prefix, list):
            if not isinstance(old_prefix, list) or len(old_prefix) != len(new_prefix):
                return [*found, Incompatibility(path, "the tuple's length changed")]
            for index, (before, after) in enumerate(zip(old_prefix, new_prefix, strict=True)):
                found += self._node(before, after, f"{path}[{index}]")
        return found


def _clean(node: object) -> Schema:
    """``node`` without the keywords in ``_DROPPED``, a one-item ``allOf`` unwrapped."""
    if not isinstance(node, dict):
        return {}
    node = {key: value for key, value in node.items() if key not in _DROPPED}
    all_of = node.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        node = {**{k: v for k, v in node.items() if k != "allOf"}, **_clean(all_of[0])}
    return node


def _ref(node: Schema) -> str | None:
    ref = node.get("$ref")
    return ref.removeprefix(_DEFS) if isinstance(ref, str) and ref.startswith(_DEFS) else None


def _expand(node: Schema, defs: object) -> Schema:
    """``node`` with the class its ``$ref`` names merged in, without its own default."""
    name = _ref(node)
    rest = {key: value for key, value in node.items() if key not in {"$ref", "default"}}
    if name is None:
        return rest
    target = _as_dict(defs).get(name)
    return {**_clean(target), **rest}


def _branches(node: Schema) -> list[object]:
    """The branches of a union, or ``node`` alone (without what belongs to the field)."""
    branches = _as_list(node.get("anyOf")) or _as_list(node.get("oneOf"))
    return branches or [_without_union(node)]


def _is_object(node: Schema) -> bool:
    return "properties" in node or _types(node) == {"object"}


def _without_union(node: Schema) -> Schema:
    return {key: value for key, value in node.items() if key not in {"default", "discriminator"}}


def _join(path: str, field: str) -> str:
    return f"{path}.{field}" if path else field
