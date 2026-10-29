"""What an operator control schema lets a sender say, field by field.

Steering must never reach measurement or acceptance. ``control_schema_fields`` lists every field
of a discriminated control union as ``(action, field, kind)``, where ``kind`` is a JSON scalar
type, a closed choice (``enum:<Name>``) or a model the caller allowed by name
(``model:<Name>``, whose own fields are listed under ``field.subfield``). Anything else (a field
of any type, an open object, a list, a union that is not one type or null) raises
``UnsafeControlSchemaError``, so a test that compares the result with an allow-list holds every
field a sender can fill to a reviewed name and type. Strategy-declared action schemas are held
to the same check.
"""

from __future__ import annotations

from typing import Any

_SCALARS = frozenset({"string", "boolean", "integer"})


class UnsafeControlSchemaError(AssertionError):
    """A control field whose spec does not pin it to a scalar, a closed choice or a known model."""


def control_schema_fields(
    schema: dict[str, Any], *, models: frozenset[str] = frozenset()
) -> frozenset[tuple[str, str, str]]:
    """Every ``(action, field, kind)`` of the discriminated union ``schema`` describes."""
    definitions: dict[str, Any] = schema.get("$defs", {})
    discriminator = schema.get("discriminator")
    if not isinstance(discriminator, dict) or "mapping" not in discriminator:
        message = "a control schema is a union discriminated by its action"
        raise UnsafeControlSchemaError(message)
    fields: set[tuple[str, str, str]] = set()
    for action, reference in discriminator["mapping"].items():
        member = definitions[reference.rsplit("/", 1)[-1]]
        fields |= {
            (action, name, kind)
            for name, kind in _model_fields(member, definitions, models, prefix="")
        }
    return frozenset(fields)


def _model_fields(
    model: dict[str, Any], definitions: dict[str, Any], models: frozenset[str], *, prefix: str
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    # Pydantic writes ``additionalProperties: false`` only for ``extra="forbid"``; a model that
    # ignores or allows unknown keys omits it, so only an explicit false is closed.
    if model.get("additionalProperties") is not False:
        message = f"{prefix or 'a member'} accepts keys it does not declare"
        raise UnsafeControlSchemaError(message)
    for name, spec in model.get("properties", {}).items():
        path = f"{prefix}{name}"
        kind = _kind(spec, definitions, models, path=path)
        found.append((path, kind))
        if kind.startswith("model:"):
            nested = definitions[kind.removeprefix("model:").removesuffix("?")]
            found += _model_fields(nested, definitions, models, prefix=f"{path}.")
    return found


def _kind(
    spec: dict[str, Any], definitions: dict[str, Any], models: frozenset[str], *, path: str
) -> str:
    if "anyOf" in spec:
        options = [option for option in spec["anyOf"] if option.get("type") != "null"]
        if len(options) != 1 or len(spec["anyOf"]) != len(options) + 1:
            message = f"{path}: a union other than one type or null"
            raise UnsafeControlSchemaError(message)
        return _kind(options[0], definitions, models, path=path) + "?"
    if "$ref" in spec:
        name = spec["$ref"].rsplit("/", 1)[-1]
        target = definitions[name]
        if "enum" in target:
            return f"enum:{name}"
        if name in models:
            return f"model:{name}"
        if _scalar(target):
            # A named constrained scalar, such as an id type: still one string or number.
            return str(target["type"])
        message = f"{path}: {name} is a structure no one allowed"
        raise UnsafeControlSchemaError(message)
    if not _scalar(spec):
        message = f"{path}: {spec} is not a string, boolean or integer"
        raise UnsafeControlSchemaError(message)
    return str(spec["type"])


def _scalar(spec: dict[str, Any]) -> bool:
    nested = ("oneOf", "allOf", "anyOf", "items", "properties", "$ref")
    return spec.get("type") in _SCALARS and not any(key in spec for key in nested)


__all__ = ["UnsafeControlSchemaError", "control_schema_fields"]
