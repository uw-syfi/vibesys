"""Emit frozen pure values from descriptor shapes."""

from __future__ import annotations

from inspect import cleandoc
from typing import TYPE_CHECKING

from protobuf.plugin import get_comments

from scripts.corevalue._cel import cel
from scripts.corevalue._constants import constants
from scripts.corevalue._imports import imports as type_imports
from scripts.corevalue._schema import (
    Options,
    extension,
    field_shape,
    is_union,
    members,
    name,
    options,
    rules,
    type_of,
    union_members,
)

if TYPE_CHECKING:
    from protobuf import DescFile


def doc(desc: object) -> str:
    """Doc."""
    return cleandoc(get_comments(desc).leading or "") or f"Immutable {desc.name} value."


def value_file(file: DescFile, settings: Options | None = None) -> str:
    """Value file."""
    settings = settings or Options()
    enums = list(file.enums)
    messages = list(file.messages)
    imports = _behavior_imports(messages) + type_imports(
        file, settings.value_package, boundary=False
    )
    lines = [
        f"# Generated from {file.name} by protoc-gen-corevalue. DO NOT EDIT.",
        "from __future__ import annotations",
        "",
        "from typing import Annotated, ClassVar, Literal, final",
        "from enum import StrEnum",
        "from pydantic import AfterValidator, ConfigDict, Field",
        "from functools import partial",
        "from pydantic.dataclasses import dataclass",
        "from vs_core._corevalue import CoreValue, FrozenMap, TimestampValue, bounded_integer",
        *imports,
        "",
    ]
    lines.extend(f"{key} = {value}" for key, value in constants(file))
    lines.append("")
    for enum in enums:
        lines.extend(_enum(enum))
    for message in messages:
        if is_union(message):
            opts = extension(message, options.ext_core_value)
            union = " | ".join(type_of(f) for f in union_members(message))
            annotation = (
                f"Annotated[{union}, Field(discriminator={opts.discriminator!r})]"
                if opts.discriminator
                else union
            )
            lines.extend([f"type {name(message)} = {annotation}", ""])
            continue
        opts = extension(message, options.ext_core_value)
        bases = f"CoreValue, {opts.mixin.rsplit('.', 1)[1]}" if opts and opts.mixin else "CoreValue"
        if opts and opts.base:
            bases = opts.base
        if not any(
            extension(child, options.ext_core_value)
            and extension(child, options.ext_core_value).base == name(message)
            for child in messages
        ):
            lines.append("@final")
        lines.extend(
            [
                "@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra='forbid', strict=True, validate_default=True))",
                f"class {name(message)}({bases}):",
                f"    {doc(message)!r}",
            ]
        )
        fields = list(message.fields)
        lines.append(
            f"    model_fields: ClassVar[tuple[str, ...]] = {tuple(f.name for f in fields)!r}"
        )
        for f in fields:
            annotation, default = _annotation(f)
            lines.append(f"    {f.local_name}: {annotation}" + (f" = {default}" if default else ""))
        checks = extension(message, rules.ext_message)
        if checks and checks.cel:
            lines.extend(
                [
                    "",
                    "    def __post_init__(self) -> None:",
                    '        """Enforce the proto message rules at construction as well as decode."""',
                ]
            )
            for rule in checks.cel:
                expression = cel(rule.expression, message)
                lines.extend(
                    [
                        f"        if not ({expression}):",
                        f"            raise ValueError({rule.message!r})",
                    ]
                )
        lines.extend(["", "    __hash__ = CoreValue.cached_hash", ""])
    return "\n".join(lines) + "\n"


def _enum(enum: object) -> list[str]:
    """Emit persisted enum spellings without the unspecified member."""
    lines = []
    values = members(enum)
    opts = extension(enum, options.ext_core_enum)
    if opts and opts.literal:
        lines.append(
            f"{'' if opts.inline else 'type '}{name(enum)} = Literal[{', '.join(repr(v) for _, v, _ in values)}]"
        )
    else:
        lines.extend([f"class {name(enum)}(StrEnum):", f"    {doc(enum)!r}"])
        lines.extend(f"    {member} = {spelling!r}" for member, spelling, _ in values)
    lines.append("")
    return lines


def _behavior_imports(messages: list) -> list[str]:
    """Import pure behavior declared by a message, independently of schema rules."""
    imports = []
    for message in messages:
        opts = extension(message, options.ext_core_value)
        if opts and opts.mixin:
            module, _, cls = opts.mixin.rpartition(".")
            imports.append(f"from {module} import {cls}")
    return imports


def _annotation(f: object) -> tuple[str, str]:
    """Apply scalar metadata in the order that preserves legacy JSON schema bounds."""
    annotation, default, constraints = field_shape(f)
    if constraints:
        metadata = f"Field({', '.join(constraints)})"
        if annotation.startswith("Annotated[int, ") and annotation.endswith("]"):
            annotation = annotation.replace("Annotated[int, ", f"Annotated[int, {metadata}, ", 1)
        else:
            annotation = f"Annotated[{annotation}, {metadata}]"
    return annotation, default
